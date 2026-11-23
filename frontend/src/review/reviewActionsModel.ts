/**
 * Pure projections of Review actions for the reader: which action addressed
 * which finding, what each status means in words, and the palette/More entries
 * built from the server's catalog. No fetching here.
 */
import type { ReviewActionAvailability, ReviewActionKind, ReviewActionRecord } from "./reviewActionsApi";
import { revisionName } from "./revisionLabel";

export interface ReviewPaletteAction {
  id: string;
  label: string;
  detail?: string;
  shortcut?: string;
  disabled?: boolean;
  /** This action moves focus into a newly opened view/dialog; skip Radix return-to-opener. */
  managesFocus?: boolean;
  run(): void;
}

export type ActionTone = "progress" | "done" | "warning" | "muted";

export interface ActionProvenance {
  tone: ActionTone;
  text: string;
}

/** The newest action per subject id: what the reader shows on each finding. */
export function latestActionBySubject(actions: readonly ReviewActionRecord[]): Map<string, ReviewActionRecord> {
  const latest = new Map<string, ReviewActionRecord>();
  for (const action of actions) {
    for (const subject of action.subjects) {
      const current = latest.get(subject.id);
      if (!current || action.created_at > current.created_at) latest.set(subject.id, action);
    }
  }
  return latest;
}

export function routeName(action: Pick<ReviewActionRecord, "route">): string {
  const route = action.route;
  return route.label || route.target_ref || route.host || "the fixer";
}

/** One line, in words, for a finding's latest action. Never claims the finding is resolved. */
export function actionProvenance(action: ReviewActionRecord): ActionProvenance {
  const label = action.label || "Fix";
  switch (action.status) {
    case "dispatching":
      return { tone: "progress", text: `${label} sending to ${routeName(action)}…` };
    case "awaiting_snapshot":
      if (action.route.mode === "copy") return { tone: "progress", text: `${label} prompt copied · waiting for a new revision` };
      if (action.route.mode === "apply") return { tone: "progress", text: `${label}: applied in your checkout · capture a revision to review it` };
      return { tone: "progress", text: `${label} sent to ${routeName(action)} · waiting for a new revision` };
    case "uncertain":
      return { tone: "warning", text: `${label} delivery to ${routeName(action)} is unconfirmed · check the agent` };
    case "snapshot_recorded": {
      const snapshot = action.resulting_revision_number != null ? revisionName(action.resulting_revision_number) : "a new revision";
      const certainty = action.result_certainty === "unattributed" ? " (not attributed to the fixer)" : "";
      return { tone: "done", text: `Addressed by ${label} → ${snapshot}${certainty} · re-review` };
    }
    case "failed":
      return { tone: "warning", text: `${label} failed${action.message ? `: ${action.message}` : ""} · the finding is unchanged` };
    case "cancelled":
      return { tone: "muted", text: `${label} cancelled · the finding is unchanged` };
  }
}

export function isActionInFlight(action: ReviewActionRecord): boolean {
  return action.status === "dispatching" || action.status === "awaiting_snapshot" || action.status === "uncertain";
}

const PALETTE_LABELS: Record<ReviewActionKind, (count: number) => string> = {
  fix: () => "Fix…",
  fix_all: (count) => (count > 0 ? `Fix all open findings (${count})…` : "Fix all open findings…"),
  fix_ci_failure: (count) => (count > 1 ? `Fix CI failures (${count})…` : "Fix CI failure…"),
  update_tests: () => "Generate or update tests…",
  resolve_merge_conflicts: () => "Resolve merge conflicts…",
  apply_suggestion: () => "Apply suggestion…",
  create_follow_up_issue: () => "Create follow-up issue…",
  org_recipe: () => "Run organization recipe…",
  revert: () => "Revert…",
  fix_forward: () => "Fix forward…",
};

function unavailableDetail(item: ReviewActionAvailability): string {
  return [item.reason, item.setup].filter(Boolean).join(" · ");
}

/**
 * Expert correction actions for the command palette and the More menu. Each
 * catalog kind appears exactly once; an unavailable one stays listed, disabled,
 * with its honest reason and setup hint rather than disappearing.
 */
export function correctionPaletteActions(
  catalog: readonly ReviewActionAvailability[],
  open: (kind: ReviewActionKind, subjectIds: string[]) => void,
  options: { busy?: boolean } = {},
): ReviewPaletteAction[] {
  return catalog.map((item) => {
    const singleSubject = item.kind === "apply_suggestion";
    const ambiguous = singleSubject && item.available && item.subject_ids.length > 1;
    return {
      id: `correction-${item.kind}`,
      label: PALETTE_LABELS[item.kind](item.subject_ids.length),
      detail: !item.available
        ? unavailableDetail(item)
        : ambiguous
          ? `${item.subject_ids.length} suggestions: apply one from its card`
          : item.code_changing
            ? "Preview the route and handoff first; the result comes back as a new revision"
            : undefined,
      disabled: !item.available || ambiguous || Boolean(options.busy),
      managesFocus: true,
      run: () => open(item.kind, singleSubject ? item.subject_ids.slice(0, 1) : [...item.subject_ids]),
    } satisfies ReviewPaletteAction;
  });
}
