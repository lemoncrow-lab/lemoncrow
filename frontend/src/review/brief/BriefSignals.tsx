import { Fragment, useId, useRef, type ReactNode } from "react";
import { Button as UiButton } from "../../components/ui/button";
import type { ReviewChangeRisk, ReviewRiskLevel, ReviewTarget } from "../types";
import { partCount } from "../vocabulary";

const LEVEL_TONE: Record<ReviewRiskLevel, string> = {
  high: "text-p0",
  medium: "text-p1",
  low: "text-foreground",
  not_measured: "text-muted-foreground",
};

const LEVEL_LABEL: Record<ReviewRiskLevel, string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
  not_measured: "Not checked",
};

/** `0 of 2 risky parts reviewed`; with nothing risky, `1 of 3 parts reviewed` (brief_summary.understanding_text). */
export function understandingText(
  reviewed: number,
  total: number,
  risky: boolean
): string {
  return risky
    ? `${reviewed} of ${total} risky ${total === 1 ? "part" : "parts"} reviewed`
    : `${reviewed} of ${partCount(total)} reviewed`;
}

/**
 * Understanding from the Reader's live marks: parts judged (Reviewed or Needs changes) among the queue's
 * Review first parts (`priority.review_first`, which a judgment does not clear); every part when there are
 * none (brief_summary.change_risk). Targets without a priority fall back to the Brief's `risky_target_ids`.
 */
export function liveUnderstanding(
  risk: ReviewChangeRisk,
  targets: readonly ReviewTarget[]
): string {
  if (targets.length === 0) return risk.understanding.text;
  const ranked = targets.some(
    (target) => typeof target.priority?.review_first === "boolean"
  );
  const risky = ranked
    ? targets.filter((target) => target.priority?.review_first === true)
    : targets.filter((target) =>
        risk.risky_target_ids.includes(target.target_id)
      );
  if (!ranked && risk.risky_target_ids.length > 0 && risky.length === 0)
    return risk.understanding.text;
  const counted = risky.length > 0 ? risky : targets;
  const reviewed = counted.filter(
    (target) => target.state === "reviewed" || target.state === "needs_changes"
  ).length;
  return understandingText(reviewed, counted.length, risky.length > 0);
}

/** Backticked code in a fact as <code>, the rest as text. */
function withCode(text: string): ReactNode {
  return text.split("`").map((piece, index) =>
    index % 2 === 1 ? (
      <code
        key={index}
        className="rounded bg-muted/70 px-1 font-mono text-subtle"
      >
        {piece}
      </code>
    ) : (
      <Fragment key={index}>{piece}</Fragment>
    )
  );
}

/** The Brief's two grounded signals and one disclosure with the facts behind them. */
export default function BriefSignals({
  risk,
  targets,
  onOpenTarget,
}: {
  risk: ReviewChangeRisk;
  targets: readonly ReviewTarget[];
  onOpenTarget: (targetId: string) => void;
}) {
  const known = new Set(targets.map((target) => target.target_id));
  const evidence = risk.facts.length + risk.not_measured.length;
  const evidenceId = useId();
  const evidenceRef = useRef<HTMLDetailsElement>(null);
  // Risk and Understanding each open the same evidence (stacked on a phone, above Needs your attention).
  const signal = (testId: string, content: ReactNode) => (
    <p data-testid={testId}>
      {evidence > 0 ? (
        <UiButton
          unstyled
          type="button"
          className="min-w-0 text-left [overflow-wrap:anywhere] underline-offset-2 hover:underline"
          aria-controls={evidenceId}
          onClick={() => {
            const details = evidenceRef.current;
            if (!details) return;
            details.open = true;
            details.scrollIntoView?.({ block: "nearest" });
          }}
        >
          {content}
        </UiButton>
      ) : (
        content
      )}
    </p>
  );
  return (
    <div
      className="flex min-w-0 flex-col gap-0.5 text-caption [overflow-wrap:anywhere]"
      data-testid="brief-signals"
    >
      {signal(
        "brief-risk",
        <>
          <span className="text-muted-foreground">Risk </span>
          <span className={`font-semibold ${LEVEL_TONE[risk.level]}`}>
            {LEVEL_LABEL[risk.level]}
          </span>
          {risk.reason && (
            <span className="text-foreground"> · {withCode(risk.reason)}</span>
          )}
        </>
      )}
      {signal(
        "brief-understanding",
        <>
          <span className="text-muted-foreground">Understanding </span>
          <span className="text-foreground">
            {liveUnderstanding(risk, targets)}
          </span>
        </>
      )}
      {evidence > 0 && (
        <details
          ref={evidenceRef}
          id={evidenceId}
          className="text-muted-foreground"
          data-testid="brief-risk-evidence"
        >
          <summary className="cursor-pointer">See the evidence</summary>
          <ul className="mt-1 flex flex-col gap-1 pl-2">
            {risk.facts.map((fact) => {
              const id = fact.target_ids.find((targetId) =>
                known.has(targetId)
              );
              const body = (
                <>
                  <span className={`font-medium ${LEVEL_TONE[fact.level]}`}>
                    {LEVEL_LABEL[fact.level]}
                  </span>
                  <span className="text-foreground">
                    {" "}
                    · {withCode(fact.text)}
                  </span>
                </>
              );
              return (
                <li
                  key={`${fact.kind}:${fact.text}`}
                  data-testid="brief-risk-fact"
                >
                  {id ? (
                    <UiButton
                      unstyled
                      type="button"
                      className="min-w-0 text-left underline-offset-2 hover:underline"
                      onClick={() => onOpenTarget(id)}
                    >
                      {body}
                    </UiButton>
                  ) : (
                    body
                  )}
                </li>
              );
            })}
            {risk.not_measured.map((text) => (
              <li
                key={`not-measured:${text}`}
                data-testid="brief-risk-not-measured"
              >{`Not checked: ${text}`}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
