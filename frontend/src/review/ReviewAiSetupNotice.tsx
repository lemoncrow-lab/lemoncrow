import type { ReviewAiSetupHelp } from "./types";

/**
 * What a person with no AI model set up is told, from the same server words as the Brief's
 * "AI review: not set up" line (one source: `lc review --ai`'s own message).
 */
export function ReviewAiSetupNotice({
  help,
  agentPicker = false,
  testId = "ask-setup-help",
}: {
  help: ReviewAiSetupHelp;
  /** True when an installed agent can be chosen under Provider instead of setting a key. */
  agentPicker?: boolean;
  testId?: string;
}) {
  return (
    <div data-testid={testId} role="status" className="space-y-1 rounded-lg border border-border bg-muted/30 px-2.5 py-2 text-caption leading-5 text-muted-foreground">
      <p>
        <span className="font-medium text-foreground">{help.headline}</span> Ask needs one, and it is set up from your terminal.
      </p>
      <ul className="space-y-0.5">
        {help.steps.map((step) => (
          <li key={step.command}>
            {step.lead}{" "}
            <code className="rounded bg-muted/70 px-1.5 py-0.5 text-caption text-subtle">{step.command}</code>{" "}
            {step.note}
          </li>
        ))}
      </ul>
      {agentPicker && <p>You can also pick an installed agent under Provider below.</p>}
      <p>
        Details:{" "}
        <a className="underline underline-offset-2" href={help.docs_url} target="_blank" rel="noreferrer noopener">
          {help.docs_url.replace(/^https?:\/\//, "")}
        </a>
      </p>
    </div>
  );
}
