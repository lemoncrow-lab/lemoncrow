import { useEffect, useState } from "react";

import { Badge } from "../../components/ui/badge";
import { fetchGitHubBotApprovals } from "../reviewApi";
import type { GitHubBotApproval } from "../types";
import { botHint } from "./githubModel";

/**
 * SPEC 4.17 (C7): a bot's approval of the pull request is "a hint, not a judgment". It answers no part and changes no
 * category, so the files come back to the reviewer. Shown only for a pull request Review, only for an approval of this
 * revision's commit; a failed lookup shows nothing, because a hint that is not there is not a fact.
 */
export function BotApprovalHint({ reviewId, enabled }: { reviewId: string; enabled: boolean }) {
  const [approvals, setApprovals] = useState<readonly GitHubBotApproval[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setApprovals([]);
    fetchGitHubBotApprovals(reviewId)
      .then((found) => { if (live) setApprovals(found.approvals); })
      .catch(() => { if (live) setApprovals([]); });
    return () => { live = false; };
  }, [reviewId, enabled]);
  const hint = botHint(approvals);
  if (!enabled || !hint) return null;
  return (
    <section aria-label="Bot approval" className="flex flex-col gap-1 rounded-lg border border-border bg-card px-3 py-2" data-testid="bot-approval">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone="ai">Bot verdict · a hint, not a judgment</Badge>
        <span className="text-label text-foreground" data-testid="bot-approval-title">{hint.title}</span>
      </div>
      <p className="text-label text-muted-foreground" data-testid="bot-approval-detail">{hint.detail}</p>
    </section>
  );
}
