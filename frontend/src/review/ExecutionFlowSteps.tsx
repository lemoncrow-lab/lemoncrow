import type { ReviewArchitectureDelta, ReviewObservedCall } from "./types";

function call(row: ReviewObservedCall): string {
  return `${row.from}:${row.from_fn} → ${row.to}:${row.to_fn}`;
}

/**
 * The changed-relationship flow. A step is "observed" only when a planned run's
 * call trace from the same Snapshot matched it (and cites that trace); every
 * other step stays labeled "inferred" from the import graph.
 */
export default function ExecutionFlowSteps({ flow }: { flow: ReviewArchitectureDelta["flow"] }) {
  const observed = flow.observed;
  return (
    <>
      <ol className="space-y-1.5 text-caption leading-4 text-neutral-400" data-testid="execution-flow">
        {flow.steps.map((step) => (
          <li key={step.edge_id} data-testid={`flow-step-${step.edge_id}`}>
            <span className="font-mono text-neutral-300">{step.from}</span> → <span className="font-mono text-neutral-300">{step.to}</span> · {step.delta_status} {step.relationship}
            {step.grounding === "observed" && step.trace ? (
              <span
                className="ml-1.5 rounded border border-emerald-500/40 px-1 text-micro text-emerald-300"
                title={`Observed on ${step.trace.side} in ${step.trace.recipe}: ${step.trace.evidence_ref}`}
                data-testid="flow-grounding-observed"
              >
                observed · {step.trace.caller_fn} → {step.trace.callee_fn}
              </span>
            ) : (
              <span className="ml-1.5 rounded border border-border px-1 text-micro text-muted-foreground" data-testid="flow-grounding-inferred">
                inferred
              </span>
            )}
          </li>
        ))}
      </ol>
      {observed?.available ? (
        <div className="mt-2 space-y-1 text-caption leading-4" data-testid="observed-sequence">
          {observed.comparable ? (
            <>
              {observed.added.map((row) => (
                <div key={`+${call(row)}`} className="font-mono text-amber-300" title={row.evidence_ref}>
                  + {call(row)}
                </div>
              ))}
              {observed.removed.map((row) => (
                <div key={`-${call(row)}`} className="font-mono text-sky-300" title={row.evidence_ref}>
                  − {call(row)}
                </div>
              ))}
            </>
          ) : (
            observed.head.map((row) => (
              <div key={call(row)} className="font-mono text-neutral-400" title={row.evidence_ref}>
                {call(row)}
              </div>
            ))
          )}
          <p className="text-muted-foreground">{observed.note}</p>
        </div>
      ) : (
        <p className="mt-2 text-caption leading-4 text-muted-foreground">This is a deterministic changed-relationship path, not a claim of runtime order.</p>
      )}
    </>
  );
}
