import type { ReviewTarget } from "./types";

/**
 * "Moved from <path>": the judgment on this part followed a symbol that moved files with exactly the same name and
 * body, and nothing else in the change matched it. Shown only for a carried move; a rename (`renamed_from`) is data for
 * the Quick check batch and is never drawn, because the body is not proof the change is routine.
 */
export function MovedFromLabel({ target }: { target: Pick<ReviewTarget, "moved_from"> }) {
  const origin = target.moved_from;
  if (!origin) return null;
  return (
    <span
      className="inline-flex h-5 min-w-0 max-w-full shrink items-center rounded-sm border border-border px-1.5 font-mono text-tag font-medium leading-none text-muted-foreground"
      title={`Same name and body as in ${origin.path}, and nothing else in the change matches it, so your judgment moved with it.`}
      data-testid="target-moved-from"
    >
      <span className="truncate">Moved from {origin.path}</span>
    </span>
  );
}
