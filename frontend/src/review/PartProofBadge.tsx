import { Badge } from "../components/ui/badge";
import { CI_SOURCE, NO_TEST_COMMAND, partOutcome, reachLine, revisionHasNoTestCommand, type PartProofProjection } from "./partProof";

/**
 * One part's proof outcome from the shared record, in the shared vocabulary. Never blank: "Not tested" and "Not run yet"
 * are neutral words, never a green pass. The caller reads the record once per screen and passes it in, so a list of rows
 * costs a map lookup each, not a subscription or a request each. `showSource={false}` where a PartProofNote under it
 * already carries the CI source line.
 */
export function PartProofBadge({ proof, path, showReason = false, showSource = true, saidOnScreen = false }: { proof: PartProofProjection | null | undefined; path: string; showReason?: boolean; showSource?: boolean; saidOnScreen?: boolean }) {
  const view = partOutcome(proof, path);
  // No test command: one quiet line, not a warning chip; a screen that already says it once (`NoTestCommandLine`) shows nothing here.
  if (view.noTestCommand) {
    return saidOnScreen ? null : <span className="text-meta text-muted-foreground" data-testid="part-proof" data-outcome={view.outcome} title={view.text}>{NO_TEST_COMMAND}</span>;
  }
  // A CI-reported result keeps the shared vocabulary but is drawn with a dashed border and always carries its source line,
  // so it is never read as a test LemonCrow ran.
  const badge = <Badge tone={view.tone} className={view.ci ? "border-dashed" : undefined} title={[view.ci, view.text, reachLine(view)].filter(Boolean).join(" ")} data-testid="part-proof" data-outcome={view.outcome} data-source={view.ci ? CI_SOURCE : undefined}>{view.label}</Badge>;
  // A hover title alone is invisible on touch and to anyone who does not hover: rows say why the tests could not run.
  const reason = showReason && view.outcome === "couldnt_run";
  const source = showSource && view.ci;
  if (!reason && !source) return badge;
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-1">
      {badge}
      {source ? <PartProofSource line={view.ci} /> : null}
      {reason ? <span className="min-w-0 break-words text-meta text-muted-foreground" data-testid="part-proof-reason">{view.text}</span> : null}
    </span>
  );
}

/**
 * The screen's one line when the revision has no test command: the parts' own badges then pass `saidOnScreen`. Renders
 * nothing otherwise, so a screen can always place it.
 */
export function NoTestCommandLine({ proof, className = "" }: { proof: PartProofProjection | null | undefined; className?: string }) {
  if (!revisionHasNoTestCommand(proof)) return null;
  return <p className={`text-meta text-muted-foreground ${className}`} data-testid="no-test-command">{NO_TEST_COMMAND}</p>;
}

/** Who ran the test, in words: shown wherever a CI-reported result is shown. */
function PartProofSource({ line }: { line: string }) {
  return <span className="min-w-0 break-words text-meta text-muted-foreground" data-testid="part-proof-source" data-source={CI_SOURCE}>{line}</span>;
}

/** The sentence under the badge on the Focus card: what the outcome means and which tests reach the part. */
export function PartProofNote({ proof, path, className = "" }: { proof: PartProofProjection | null | undefined; path: string; className?: string }) {
  const view = partOutcome(proof, path);
  if (view.noTestCommand) return null;
  return <p className={`text-meta text-muted-foreground ${className}`} data-testid="part-proof-note">{[view.text, reachLine(view)].filter(Boolean).join(" ")}{view.ci ? <> <PartProofSource line={view.ci} /></> : null}</p>;
}
