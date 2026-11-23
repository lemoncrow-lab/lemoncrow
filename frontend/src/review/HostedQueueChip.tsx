import { Cloud } from "lucide-react";

import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import type { FlowHostedQueue } from "./flowReviewTools";
import { HostedQueuePanel } from "./ReviewHostedQueue";
import type { HostedAttachmentStatus } from "./types";

/** Queued and conflicting actions, from the server's counts, else from the listed actions. */
export function hostedCounts(status: HostedAttachmentStatus | null): { queued: number; conflicts: number } {
  const actions = status?.actions ?? [];
  return {
    queued: status?.counts?.pending ?? actions.filter((action) => action.state === "pending").length,
    conflicts: status?.counts?.conflict ?? actions.filter((action) => action.state === "conflict").length,
  };
}

/** "2 queued · 1 conflict"; empty when nothing waits. */
export function hostedQueueText(counts: { queued: number; conflicts: number }): string {
  return [
    counts.queued > 0 ? `${counts.queued} queued` : "",
    counts.conflicts > 0 ? `${counts.conflicts} conflict${counts.conflicts === 1 ? "" : "s"}` : "",
  ].filter(Boolean).join(" · ");
}

/**
 * On a review attached to hosted: "Hosted · 2 queued · 1 conflict" in the top bar, opening the hosted queue (re-apply,
 * discard, detach). Conflicts and an unreadable queue are warnings: hosted refused something and only the reviewer
 * can decide what happens to it.
 */
export function HostedQueueChip({ reviewId, hosted }: { reviewId: string; hosted: FlowHostedQueue }) {
  const counts = hostedCounts(hosted.status);
  const text = hostedQueueText(counts);
  const warn = counts.conflicts > 0 || Boolean(hosted.error);
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="shrink-0 gap-1 px-2"
          title={`Attached to hosted ${hosted.info.reference}`}
          aria-label={`Hosted ${hosted.info.reference}${text ? ` · ${text}` : ""}${hosted.error ? " · queue unavailable" : ""}`}
          data-testid="hosted-queue-chip"
        >
          <Cloud size={16} aria-hidden="true" className={warn ? "text-p1" : "text-p2"} />
          <span className="max-md:hidden">Hosted</span>
          {counts.queued > 0 ? <Badge tone="info">{counts.queued} queued</Badge> : null}
          {counts.conflicts > 0 ? <Badge tone="warning">{counts.conflicts} conflict{counts.conflicts === 1 ? "" : "s"}</Badge> : null}
          {hosted.error && counts.conflicts === 0 ? <Badge tone="warning">Queue unavailable</Badge> : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" aria-label="Hosted queue" className="w-[min(460px,calc(100vw-2rem))]">
        <HostedQueuePanel
          reviewId={reviewId}
          hosted={hosted.info}
          status={hosted.status}
          error={hosted.error}
          onStatus={hosted.onStatus}
          onDetached={hosted.onDetached}
        />
      </PopoverContent>
    </Popover>
  );
}
