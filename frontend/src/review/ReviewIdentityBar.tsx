import { useEffect, useRef, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { Clock3 } from "lucide-react";

import { fetchChangeSet, type ChangeSetView } from "./changeSetApi";
import {
  fallbackReviewEnvironment,
  reviewEnvironmentDescription,
  reviewEnvironmentLabel,
  type ReviewEnvironment,
} from "./reviewEnvironment";
import { webHref } from "./safeLinks";
import { revisionShortLabel } from "./revisionLabel";
import ReviewBaseIdentity from "./ReviewBaseIdentity";
import type { ReviewLandingReadiness, ReviewOverview, ReviewStackInfo, ReviewStackMember } from "./types";

function stackMemberName(member: ReviewStackMember): string {
  if (member.provider !== "git" && member.external_id) return member.external_id;
  if (member.change_ref) return member.change_ref.slice(0, 10);
  return member.head_ref || member.external_id || member.head_sha.slice(0, 8);
}

/** Read-only stack position for the Review header (Phase 9 item 24). */
export function stackNeedsHeaderAttention(stack: ReviewStackInfo): boolean {
  if (!stack.complete) return true;
  return stack.lower.some((member) => !["approved", "landed", "closed"].includes(member.readiness.state));
}

export function stackSummary(stack: ReviewStackInfo): { label: string; title: string } {
  const below = stack.lower[0];
  const parts = [`Layer ${stack.layer} of ${stack.depth}${stack.complete ? "" : "+"}`];
  if (below) parts.push(`on ${stackMemberName(below)} (${below.readiness.label})`);
  if (stack.upper.length > 0) parts.push(`${stack.upper.length} above`);
  const describe = (member: ReviewStackMember, where: string) =>
    `${where} ${stackMemberName(member)}${member.title ? ` ${member.title}` : ""}: ${member.readiness.label}`;
  const lines = [
    ...stack.lower.map((member) => describe(member, member.distance === 1 ? "Below:" : "Further below:")),
    ...stack.upper.map((member) => describe(member, "Above:")),
  ];
  if (stack.base.kind === "lower_change") lines.push("This diff starts at the lower layer's head, so its work is not shown as this Change's.");
  if (stack.note) lines.push(stack.note);
  lines.push("Read-only: LemonCrow never rebases, retargets or restacks branches.");
  return { label: parts.join(" · "), title: lines.join("\n") };
}

const CHANGE_SET_STATE: Record<string, string> = {
  ready: "ready",
  blocked: "blocked",
  needs_decision: "deploy order needs a decision",
  incomplete: "incomplete",
};

/** Multi-repository change-set position for the Review header (Phase 9R item 42). */
export function changeSetSummary(set: ChangeSetView): { label: string; title: string; blocked: boolean } {
  const names = new Map(set.members.filter((member) => member.readable).map((member) => [member.alias, member.repository ?? ""]));
  const name = (alias: string) => names.get(alias) || "a member";
  const label = `Change set · ${set.member_count} Changes · ${CHANGE_SET_STATE[set.state] ?? set.state}`;
  const lines = set.members
    .filter((member) => member.readable)
    .map((member) => {
      const readiness =
        member.current ? "this Change" : member.ready === true ? "ready" : member.ready === null ? "readiness unavailable" : `${member.blockers ?? 0} blocker(s)`;
      return `${member.repository} ${member.ref ?? ""}${member.title ? ` ${member.title}` : ""}: ${readiness}`;
    });
  if (set.unreadable > 0) lines.push(`${set.unreadable} member(s) you cannot read; the set is incomplete.`);
  if (set.truncated) lines.push(`The set walk stopped at ${set.max_members} members.`);
  for (const repository of set.duplicate_repositories ?? []) {
    lines.push(`More than one Change of ${repository} is in the set; unlink all but one.`);
  }
  for (const issue of set.contracts.issues.slice(0, 3)) lines.push(`${name(issue.provider)} removes ${issue.contract}, which ${name(issue.consumer)} still uses.`);
  if (set.deploy_order.state === "ordered" && set.deploy_order.steps.length > 0) {
    lines.push("Deploy order: " + set.deploy_order.steps.map((step) => `${step.position}. ${step.repository} ${step.phase}`).join(" → "));
  } else if (set.deploy_order.state === "needs_decision") {
    lines.push("Deploy order needs a decision: " + set.deploy_order.cycle.map((step) => `${name(step.alias)} ${step.phase}`).join(", ") + " depend on each other.");
  }
  return { label, title: lines.join("\n"), blocked: set.state !== "ready" };
}

/**
 * The change-set state the server's landing readiness carries on its
 * `change-set:*` conditions. It is the live source: it refreshes with every
 * readiness refetch, so the header and the readiness sheet never disagree.
 */
export function changeSetStateFromLanding(landing: ReviewLandingReadiness | null | undefined): string | null {
  const row = landing?.conditions.find((condition) => condition.id.startsWith("change-set:") && condition.set_state);
  return row?.set_state ?? null;
}

function ChangeSetLabel({ reviewId, links, landing }: { reviewId: string; links: number; landing?: ReviewLandingReadiness | null }) {
  const [set, setSet] = useState<ChangeSetView | null>(null);
  const [failed, setFailed] = useState(false);
  // Readiness of another Review (a navigation whose readiness is still loading) is not this Change's.
  const own = landing && landing.review_id === reviewId ? landing : null;
  const liveState = changeSetStateFromLanding(own);
  // The member details refetch when the server's readiness conditions really change. The
  // last hash is kept while readiness reloads (no landing meanwhile), and the first hash to
  // arrive is no change: the details fetched on mount are already current.
  const lastHash = useRef({ reviewId, hash: "" });
  if (lastHash.current.reviewId !== reviewId) lastHash.current = { reviewId, hash: "" };
  if (own?.conditions_hash) lastHash.current = { reviewId, hash: own.conditions_hash };
  const refreshKey = lastHash.current.hash;
  const fetched = useRef<{ reviewId: string; links: number; hash: string } | null>(null);
  const request = useRef(0);
  useEffect(
    () => () => {
      request.current += 1; // unmounted: a pending answer is dropped
    },
    [],
  );
  useEffect(() => {
    const previous = fetched.current;
    fetched.current = { reviewId, links, hash: refreshKey };
    if (previous?.reviewId === reviewId && previous.links === links && previous.hash === "" && refreshKey !== "") return;
    const id = ++request.current;
    setFailed(false);
    fetchChangeSet(reviewId).then(
      (view) => {
        if (request.current === id) setSet(view);
      },
      () => {
        if (request.current === id) setFailed(true);
      },
    );
  }, [reviewId, links, refreshKey]);
  const summary = set?.linked ? changeSetSummary(set) : null;
  const state = liveState ?? (summary ? set?.state ?? null : null);
  // A healthy linked set is context, not a permanent warning in the Reader header.
  // The full set remains available from its dedicated surface; the header speaks
  // only when the set is incomplete/blocked or could not be read safely.
  const unavailable = failed && liveState === null;
  if (!unavailable && (state === null || state === "ready")) return null;
  const label = unavailable ? "Change set unavailable" : `Change set · ${CHANGE_SET_STATE[state ?? ""] ?? state ?? "blocked"}`;
  return (
    <span
      className="review-reader-source-context ml-2 max-w-[220px] truncate border-l border-border pl-2 font-medium text-amber-300/80"
      title={summary?.title ?? "The change set could not be loaded; its readiness fails closed."}
      aria-label={"Change set: " + label}
      data-testid="review-change-set"
    >
      {label}
    </span>
  );
}

interface ReviewIdentityBarProps {
  overview: ReviewOverview;
  focusMode: boolean;
  authorshipOverride?: { prefix: string; label: string };
  contextOverride?: { label: string; title?: string };
  environment?: ReviewEnvironment;
  /** Current landing readiness: the live source of the change-set header state. */
  landing?: ReviewLandingReadiness | null;
  onShowOverview(): void;
  onShowHistory(): void;
}

export default function ReviewIdentityBar({
  overview,
  focusMode,
  authorshipOverride,
  contextOverride,
  environment,
  landing,
  onShowOverview,
  onShowHistory,
}: ReviewIdentityBarProps) {
  const story = overview.change_story?.length
    ? `${overview.change_story.join(" · ")}${(overview.change_story_more ?? 0) > 0 ? ` · +${overview.change_story_more} more` : ""}`
    : overview.title || "Review";
  const authorship = authorshipOverride;
  const resolvedEnvironment = environment ?? fallbackReviewEnvironment("local");
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
  const stack = overview.change?.stack ? stackSummary(overview.change.stack) : null;
  const workItem = (overview.change?.work_items ?? [])[0];
  const workItemLabel = workItem ? (workItem.external_id || workItem.title || "Work item") : "";
  const workspaceContext = contextOverride?.label
    ? contextOverride
    : resolvedEnvironment.review.local_workspace && overview.workspace_root
      ? { label: overview.workspace_root, title: `Workspace ${overview.workspace_root}` }
      : null;

  return (
    <div className="review-reader-identity">
      <UiButton unstyled
        type="button"
        onClick={onShowOverview}
        aria-label="Open change overview"
        className="review-reader-title"
        tooltip={`${story} · Open change overview`}
      >
        {focusMode ? "Review" : story}
      </UiButton>
      {!focusMode && (
        <span className="review-reader-authorship">
          {authorship && (authorship.prefix || authorship.label) && (
            <span>
              {authorship.prefix && <span className="review-reader-author">{authorship.prefix} </span>}
              <span className="review-reader-author font-medium text-neutral-400">{authorship.label}</span>
            </span>
          )}
          {stack && overview.change?.stack && stackNeedsHeaderAttention(overview.change.stack) && (
            <span
              className="review-reader-source-context ml-2 max-w-[260px] truncate border-l border-border pl-2 font-medium text-amber-300/80"
              title={stack.title}
              aria-label={"Stack needs attention: " + stack.label}
              data-testid="review-stack-layer"
            >
              {overview.change.stack.complete ? "Stack blocked" : "Stack incomplete"}
            </span>
          )}
          {overview.change?.change_set?.linked && (
            <ChangeSetLabel reviewId={overview.session.id} links={overview.change.change_set.links} landing={landing} />
          )}
          {linkedChangeLabel && (
            <span
              className="review-reader-source-context ml-2 max-w-[220px] truncate border-l border-border pl-2 font-medium text-muted-foreground"
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
                className="review-reader-source-context ml-2 max-w-[180px] truncate border-l border-border pl-2 font-medium text-muted-foreground hover:text-neutral-300 hover:underline"
                title={workItem.title || workItem.external_id}
                aria-label={`Open associated work item ${workItemLabel}`}
              >
                {workItemLabel}
              </a>
            ) : (
              <span
                className="review-reader-source-context ml-2 max-w-[180px] truncate border-l border-border pl-2 font-medium text-muted-foreground"
                title={workItem.title || workItem.external_id}
              >
                {workItemLabel}
              </span>
            )
          )}
          <span
            className="review-reader-mode-context ml-2 border-l border-border pl-2 font-medium text-muted-foreground"
            title={reviewEnvironmentDescription(resolvedEnvironment)}
            aria-label={"Review environment: " + reviewEnvironmentLabel(resolvedEnvironment.mode)}
          >
            {reviewEnvironmentLabel(resolvedEnvironment.mode)}
          </span>
          {workspaceContext?.label && (
            <span
              className="review-reader-repo-context ml-2 max-w-[220px] truncate border-l border-border pl-2 font-mono text-caption text-muted-foreground"
              title={workspaceContext.title || workspaceContext.label}
              aria-label={contextOverride?.label ? `Review context: ${workspaceContext.label}` : `Review workspace: ${workspaceContext.label}`}
            >
              {workspaceContext.label}
            </span>
          )}
          <ReviewBaseIdentity overview={overview} />
        </span>
      )}
      <UiButton
        variant="ghost"
        size="compact"
        type="button"
        onClick={onShowHistory}
        className="review-reader-revision"
        aria-label={`${revisionShortLabel(overview.revision)}; open Snapshot history and compare`}
        tooltip={
          overview.session.range_mode === "working_tree" || overview.session.range_mode === "staged"
            ? "This Snapshot is frozen. New source edits stay separate until a newer Snapshot is captured. Open Snapshot history and compare."
            : "Snapshot history and compare"
        }
      >
        <Clock3 size={12} />
        <span className="font-mono text-caption">{revisionShortLabel(overview.revision)}</span>
      </UiButton>
    </div>
  );
}
