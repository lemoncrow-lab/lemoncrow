import { RefreshCw, WifiOff } from "lucide-react";

import { Button } from "./ui/button";
import { EmptyState } from "./ui/empty-state";

/** The command that brings the local server back (the same one the CLI prints). */
export const LOCAL_SERVER_RESTART = "bash ~/.lemoncrow/install/scripts/local_server.sh restart";

/** True when a request never reached a server (browser fetch rejects with a TypeError), not an HTTP error. */
export function isUnreachable(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  const message = error instanceof Error ? error.message : String(error ?? "");
  return /failed to fetch|networkerror|load failed|err_connection/i.test(message);
}

/** A message for people: the unreachable case gets a sentence instead of the browser's "Failed to fetch". */
export function describeError(error: unknown): string {
  if (isUnreachable(error)) return "Can't reach LemonCrow on this machine.";
  return error instanceof Error ? error.message : String(error);
}

/**
 * The one "server is down" state: what happened, the command that fixes it, and Retry.
 * Without `onRetry`, Retry reloads the page.
 */
export function ServerUnreachable({ onRetry, className }: { onRetry?: () => void; className?: string }) {
  return (
    <EmptyState
      className={className}
      icon={<WifiOff aria-hidden="true" />}
      title="Can't reach LemonCrow on this machine"
      description={
        <div role="alert" className="space-y-2">
          <p>The local server isn't answering. Start it again, then retry:</p>
          <code className="block break-all rounded-md bg-muted px-2 py-1.5 text-left font-mono text-code text-foreground">
            {LOCAL_SERVER_RESTART}
          </code>
          <p>
            <code className="font-mono text-code">lc doctor</code> says why it stopped.
          </p>
        </div>
      }
      action={
        <Button variant="outline" size="sm" onClick={() => (onRetry ? onRetry() : window.location.reload())}>
          <RefreshCw size={14} aria-hidden="true" /> Retry
        </Button>
      }
    />
  );
}
