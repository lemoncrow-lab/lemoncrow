/** P6-3 (SPEC 4.17): the words and shapes of the GitHub preview. Pure; the server decides what is posted. */
import type { GitHubBotApproval, GitHubPublishPreview } from "../types";

export type GitHubVerdictChoice = "lgtm" | "lgtm_with_suggestions" | "changes_requested" | "comment";

/** The review header's verdict. It follows the event GitHub will receive, so a downgrade to a comment reads "Commented". */
export function githubVerdictLabel(event: GitHubPublishPreview["event"], choice: GitHubVerdictChoice | null): string {
  if (event === "REQUEST_CHANGES") return "Requested changes";
  if (event === "APPROVE") return choice === "lgtm_with_suggestions" ? "Approved with suggestions" : "Approved";
  return "Commented";
}

/** `#412` from a destination label such as "PR #412"; the plain words when no number is known. */
export function pullRequestTarget(label: string): string {
  const found = /#(\d+)/.exec(label);
  return found ? `#${found[1]}` : "the pull request";
}

/** `path:42`, `path:42-45`, with "(old)" for a line GitHub shows on the left. */
export function inlineLocation(item: GitHubPublishPreview["inline"][number]): string {
  const span = item.start_line === item.line ? `${item.line}` : `${item.start_line}-${item.line}`;
  return `${item.path}:${span}${item.side === "LEFT" ? " (old)" : ""}`;
}

/** The exact text posted: the verdict, the body as GitHub shows it, then each inline comment on its line. */
export function githubSentText(preview: GitHubPublishPreview, choice: GitHubVerdictChoice | null): string {
  const head = githubVerdictLabel(preview.event, choice);
  const inline = preview.inline.map((item) => `${inlineLocation(item)}\n${item.body}`);
  return [head, preview.body ?? preview.summary, ...inline].filter((part) => part.trim() !== "").join("\n\n");
}

/** "a", "a and b", "a, b and c". */
export function nameList(names: readonly string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * Bot approvals on this commit, as a hint (SPEC 4.17). An approval left on an earlier commit says nothing about this
 * revision and is not shown. GitHub has no per-file approval, so no file count is claimed.
 */
export function botHint(approvals: readonly GitHubBotApproval[]): { title: string; detail: string } | null {
  const names = [...new Set(approvals.filter((item) => item.current).map((item) => item.name))];
  if (names.length === 0) return null;
  return {
    title: `${nameList(names)} approved this pull request`,
    detail: "Its files come back to you as quick checks or decisions. Nothing was accepted on its behalf.",
  };
}
