import { useEffect, useRef, useState, type ReactNode } from "react";
import { X } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";
import { Input as UiInput } from "../components/ui/input";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetLede, SheetTitle } from "../components/ui/sheet";
import { ToggleGroup } from "../components/ui/toggle-group";
import { usePrincipalLabel } from "./plainWords";
import { revisionName } from "./revisionLabel";
import { judgedParts, partsLeft } from "./vocabulary";
import type {
  FeedbackRouteKind,
  ReviewClosure,
  ReviewOutcome,
  ReviewOutcomeKind,
} from "./types";

import "./reviewUi.css";

/**
 * What a finished review signs, from data the Reader already holds: the exact
 * revision and commit, the targets and their marks, the checks that passed and
 * the readiness conditions still unmet. Counts come from `summary`.
 */
export interface FinishRecordFacts {
  revisionNumber: number;
  /** Head commit of the revision; "" when the capture has none. */
  headSha: string;
  /** The capture includes uncommitted changes on top of `headSha`. */
  dirty?: boolean;
  /** `needsChanges`: marked Needs changes on this revision, which counts as reviewed. */
  targets: readonly { id: string; label: string; path: string; reviewed: boolean; needsChanges?: boolean }[];
  passedChecks: readonly { id: string; label: string; source: string }[];
  /** Parts still needing a look, in the engine's review order (risky ones first). */
  openParts?: readonly { id: string; label: string; path: string; reason: string; risky: boolean }[];
  /** Unmet readiness conditions beyond target marks and comments (counted separately). */
  openConditions: readonly { id: string; label: string }[];
  /** Analyses that could not run or measure on this revision. */
  notMeasured?: number;
  /** Who records the verdict. */
  reviewer: string;
  /** Verified human notes whose previous Looks good answer was cleared after seeing Structure. */
  reconsideredNotes?: readonly { id: string; path: string; body: string }[];
}

/** Where pending feedback goes if Finish also delivers it. */
export interface FinishHandoff {
  unsent: number;
  label: string;
  kind: FeedbackRouteKind;
  available: boolean;
  reason: string;
}

export interface FinishOptions {
  /** Deliver pending feedback before the outcome/finish write. */
  send: boolean;
  /** Optional human-chosen Wrap up presentation. Classic remains unchanged. */
  presentation?: import("./types").FeedbackPresentation;
  /** A changed reviewed message prevents delivery. */
  expectedFeedbackHash?: string;
}

/** Finish is a decision surface: it shows what is reviewed and what is still open; Review Status owns diagnostic detail. */
interface FinishSheetProps {
  summary: ReviewClosure;
  record: FinishRecordFacts;
  busy: boolean;
  /** The verdict just recorded; the sheet then confirms it and offers the record download. */
  recorded?: ReviewOutcomeKind | null;
  /** Export and download the Verified Change record; resolves to how the record reads. */
  onDownloadRecord?(): Promise<string>;
  currentOutcome?: ReviewOutcome | null;
  handoff?: FinishHandoff | null;
  destinationControl?: ReactNode;
  sendError?: string;
  finishError?: string;
  onClose(): void;
  onFinish(outcome: ReviewOutcomeKind, summary: string, options: FinishOptions): void;
  /** Open the authoritative status/readiness projection (which analyses could not run). */
  onOpenStatus?(): void;
  /** Close this sheet and show one still-open part in the code. */
  onOpenPart?(targetId: string): void;
  onCloseAutoFocus?: (event: Event) => void;
}

const OUTCOMES: { id: ReviewOutcomeKind; label: string; hint: string }[] = [
  { id: "comment", label: "Comment", hint: "Finish without an overall approval verdict." },
  { id: "lgtm", label: "Approve", hint: "Approve this revision." },
  { id: "changes_requested", label: "Request changes", hint: "Record requested changes and keep the Review open for re-review." },
];

function count(n: number, unit: string, units = `${unit}s`): string {
  return `${n} ${n === 1 ? unit : units}`;
}

function plural(n: number): string {
  return count(n, "comment");
}

/** Inspected targets listed inline; the rest are named by count, never dropped silently. */
const TARGET_LIST_LIMIT = 100;
/** Still-open parts named in the Still open row; the rest are counted. */
const OPEN_PART_LIMIT = 5;

function RecordGroup({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-3 py-1.5">
      <dt className="review-kicker pt-px">{term}</dt>
      <dd className="min-w-0 text-caption leading-5 text-foreground">{children}</dd>
    </div>
  );
}

function FinishRecord({
  summary,
  record,
  verdict,
  onOpenStatus,
  onOpenPart,
}: {
  summary: ReviewClosure;
  record: FinishRecordFacts;
  /** The verdict the person picked; null until they pick one, so nothing reads as decided. */
  verdict: string | null;
  onOpenStatus?: () => void;
  onOpenPart?: (targetId: string) => void;
}) {
  const principalName = usePrincipalLabel();
  // Reviewed or Needs changes counts as reviewed; "not reviewed" is every part with no judgment,
  // changed-since-review parts included.
  const marks = { target_count: summary.target_count, reviewed: summary.reviewed_targets, needs_changes: summary.needs_changes };
  const judged = judgedParts(marks);
  const notReviewed = partsLeft(marks);
  const comments = summary.open_comments + summary.orphaned_comments;
  const open = [
    notReviewed > 0 && `${count(notReviewed, "part not reviewed", "parts not reviewed")}${summary.changed_since_review > 0 ? ` (${summary.changed_since_review} changed since review)` : ""}`,
    summary.needs_changes > 0 && count(summary.needs_changes, "needs-changes mark"),
    comments > 0 && count(comments, "unresolved comment"),
  ].filter((line): line is string => Boolean(line));
  const shown = record.targets.slice(0, TARGET_LIST_LIMIT);
  const openParts = record.openParts ?? [];
  const namedParts = openParts.slice(0, OPEN_PART_LIMIT);
  const notMeasured = record.notMeasured ?? 0;
  return (
    <section aria-label="What you are signing">
      <div className="text-caption text-muted-foreground" data-testid="finish-revision">
        {revisionName(record.revisionNumber)}
        {record.headSha ? (
          <>
            {" · "}
            <span className="font-mono text-subtle">{record.headSha.slice(0, 7)}</span>
            {record.dirty && " + uncommitted changes"}
          </>
        ) : record.dirty && " · uncommitted changes"}
      </div>
      <dl className="mt-1 divide-y divide-border/50">
        <RecordGroup term="Inspected">
          {shown.length > 0 ? (
            <details>
              <summary className="cursor-pointer">
                {judged} of {count(summary.target_count, "part")} reviewed
              </summary>
              <ul className="mt-1 space-y-0.5" aria-label="Parts">
                {shown.map((target) => (
                  <li key={target.id} className="flex min-w-0 gap-2">
                    <span className={`w-24 shrink-0 ${target.reviewed ? "text-emerald-300" : target.needsChanges ? "text-red-300" : "text-muted-foreground"}`}>
                      {target.reviewed ? "reviewed" : target.needsChanges ? "needs changes" : "not reviewed"}
                    </span>
                    <span className="min-w-0 truncate" title={`${target.path} · ${target.label}`}>
                      {target.label}
                      <span className="text-muted-foreground"> · {target.path}</span>
                    </span>
                  </li>
                ))}
                {record.targets.length > shown.length && (
                  <li className="text-muted-foreground">{count(record.targets.length - shown.length, "more part")} in the Code view</li>
                )}
              </ul>
            </details>
          ) : (
            <span>{judged} of {count(summary.target_count, "part")} reviewed</span>
          )}
        </RecordGroup>
        {(record.reconsideredNotes?.length ?? 0) > 0 && <RecordGroup term="Notes">
          <ul className="space-y-2" aria-label="Notes after Structure">
            {record.reconsideredNotes!.map((note) => <li key={note.id} className="space-y-1">
              <p className="break-all font-mono text-meta text-muted-foreground">{note.path} · Answer changed after seeing structure</p>
              <p className="whitespace-pre-wrap break-words">{note.body}</p>
            </li>)}
          </ul>
        </RecordGroup>}
        <RecordGroup term="Checked">
          {record.passedChecks.length > 0 ? (
            <ul aria-label="Checks that passed">
              {record.passedChecks.map((check) => (
                <li key={check.id} className="min-w-0 truncate">
                  {check.label}
                  <span className="text-muted-foreground"> · {check.source}</span>
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-muted-foreground">No check passed on this revision</span>
          )}
        </RecordGroup>
        <RecordGroup term="Still open">
          {open.length > 0 || record.openConditions.length > 0 ? (
            <ul aria-label="Still open">
              {open.map((line) => <li key={line} className="text-amber-300">{line}</li>)}
              {record.openConditions.length > 0 && (
                <li className="text-muted-foreground">{record.openConditions.map((item) => item.label).join(" · ")}</li>
              )}
            </ul>
          ) : (
            <span className="text-emerald-300">Nothing open</span>
          )}
          {namedParts.length > 0 && (
            <ul className="mt-1 space-y-0.5" aria-label="Parts still open">
              {namedParts.map((part) => (
                <li key={part.id} className="flex min-w-0 items-baseline gap-2">
                  {part.risky && <span className="shrink-0 font-semibold text-p1">Look closely</span>}
                  {onOpenPart ? (
                    <UiButton
                      variant="link"
                      size="compact"
                      type="button"
                      className="min-w-0 justify-start truncate text-caption"
                      title={part.reason ? `${part.path} · ${part.reason}` : part.path}
                      onClick={() => onOpenPart(part.id)}
                    >
                      {part.label}
                      <span className="text-muted-foreground"> · {part.path}</span>
                    </UiButton>
                  ) : (
                    <span className="min-w-0 truncate">{part.label}<span className="text-muted-foreground"> · {part.path}</span></span>
                  )}
                </li>
              ))}
              {openParts.length > namedParts.length && (
                <li className="text-muted-foreground">{count(openParts.length - namedParts.length, "more part")} in the Code view</li>
              )}
            </ul>
          )}
          {notMeasured > 0 && (
            <p className="mt-1 text-muted-foreground">
              {count(notMeasured, "analysis", "analyses")} could not run.
              {onOpenStatus && (
                <>
                  {" "}
                  <UiButton variant="link" size="compact" type="button" className="text-caption" onClick={onOpenStatus}>
                    See which
                  </UiButton>
                </>
              )}
            </p>
          )}
        </RecordGroup>
        {verdict && (
          <RecordGroup term="Decided">
            {verdict} <span className="text-muted-foreground">· by {principalName(record.reviewer, "you")}</span>
          </RecordGroup>
        )}
      </dl>
    </section>
  );
}

function requestChangesLabel(handoff: FinishHandoff | null): string {
  if (handoff?.kind === "github") {
    return handoff.unsent > 0 ? `Request changes & publish ${handoff.unsent}…` : "Request changes & publish…";
  }
  if (!handoff || handoff.unsent === 0) return "Request changes";
  return handoff.kind === "export"
    ? `Request changes & copy ${handoff.unsent}`
    : `Request changes & send ${handoff.unsent}`;
}

function deliveryVerb(handoff: FinishHandoff): string {
  if (handoff.kind === "export") return "copy";
  if (handoff.kind === "github") return "publish";
  return "send";
}

export default function FinishSheet({
  summary,
  record,
  busy,
  recorded = null,
  onDownloadRecord,
  currentOutcome = null,
  handoff = null,
  destinationControl,
  sendError = "",
  finishError = "",
  onClose,
  onFinish,
  onOpenStatus,
  onOpenPart,
  onCloseAutoFocus,
}: FinishSheetProps) {
  const current = currentOutcome && !currentOutcome.stale ? currentOutcome : null;
  // Nothing is chosen until the person picks: finishing without a pick records a plain Comment.
  const [picked, setOutcome] = useState<ReviewOutcomeKind | null>(current?.outcome ?? null);
  const outcome: ReviewOutcomeKind = picked ?? "comment";
  const [outcomeSummary, setOutcomeSummary] = useState(current?.summary ?? "");
  const [alsoSend, setAlsoSend] = useState(true);
  const [download, setDownload] = useState<{ state: "idle" | "busy" | "done" | "failed"; text: string }>({ state: "idle", text: "" });
  const doneRef = useRef<HTMLButtonElement>(null);
  const requestChanges = outcome === "changes_requested";
  const unsent = handoff?.unsent ?? 0;
  const reachable = Boolean(handoff?.available);
  const toPullRequest = handoff?.kind === "github";
  const offerSend = Boolean(handoff) && reachable && (unsent > 0 || toPullRequest);
  const sending = offerSend && (requestChanges || alsoSend);

  const outstandingTargets =
    summary.unreviewed_targets
    + summary.changed_since_review
    + summary.needs_changes
    + summary.unknown_targets
    + (summary.later_targets ?? 0);
  const outstandingSignals =
    summary.open_comments
    + summary.orphaned_comments
    + summary.failed_verification
    + summary.unresolved_verification;
  const blockers = outstandingTargets + outstandingSignals + record.openConditions.length;
  const complete = blockers === 0;
  const blockedRequest = requestChanges && (
    (unsent > 0 && !reachable)
    || (unsent === 0 && summary.open_comments === 0 && !outcomeSummary.trim() && handoff?.kind !== "github")
  );
  const finishLabel = requestChanges
    ? requestChangesLabel(handoff)
    : outcome === "lgtm"
      ? complete ? "Approve & finish" : "Approve & finish anyway"
      : complete ? "Finish" : "Finish anyway";
  const finishingAnyway = !complete && !requestChanges;

  // Keep the explicit choice stable when an older verdict becomes stale.
  useEffect(() => {
    if (currentOutcome?.stale) {
      setOutcome(null);
      setOutcomeSummary("");
    }
  }, [currentOutcome?.stale]);

  // The verdict controls leave once it is recorded; keep focus inside the sheet.
  useEffect(() => {
    if (recorded) doneRef.current?.focus();
  }, [recorded]);

  const verdictLabel = (kind: ReviewOutcomeKind) => OUTCOMES.find((item) => item.id === kind)?.label ?? kind;
  // A Comment verdict is not a decision, so it has no Verified Change record.
  const offerRecord = Boolean(recorded && recorded !== "comment" && onDownloadRecord);
  const runDownload = async () => {
    if (!onDownloadRecord) return;
    setDownload({ state: "busy", text: "" });
    try {
      setDownload({ state: "done", text: await onDownloadRecord() });
    } catch (reason) {
      setDownload({ state: "failed", text: reason instanceof Error ? reason.message : String(reason) });
    }
  };

  if (recorded) {
    return (
      <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
        <SheetContent onCloseAutoFocus={onCloseAutoFocus}>
          <SheetHeader>
            <SheetTitle>{recorded === "changes_requested" ? "Changes requested" : "Review finished"}</SheetTitle>
            <SheetClose asChild>
              <UiButton variant="ghost" size="icon" type="button" aria-label="Close review outcome">
                <X size={14} />
              </UiButton>
            </SheetClose>
          </SheetHeader>
          <SheetLede>
            {recorded === "changes_requested"
              ? "Your verdict is recorded. The Review stays open; the next revision returns here for re-review."
              : "Your verdict is recorded. Finishing does not commit or merge."}
          </SheetLede>
          <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
            <FinishRecord summary={summary} record={record} verdict={verdictLabel(recorded)} />
            {download.state === "done" && (
              <p role="status" className="mt-3 text-caption text-muted-foreground">Downloaded: {download.text}</p>
            )}
            {download.state === "failed" && (
              <p role="alert" className="mt-3 text-caption text-red-300">Record download failed: {download.text}</p>
            )}
          </div>
          <div className="flex flex-wrap justify-end gap-2 border-t border-border/80 bg-background/95 px-4 py-3">
            {offerRecord && (
              <UiButton variant="outline" type="button" disabled={download.state === "busy"} onClick={() => void runDownload()}>
                {download.state === "busy" ? "Preparing record…" : "Download Verified Change record"}
              </UiButton>
            )}
            <UiButton ref={doneRef} variant="solid" type="button" onClick={onClose}>
              Done
            </UiButton>
          </div>
        </SheetContent>
      </Sheet>
    );
  }

  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent onCloseAutoFocus={onCloseAutoFocus}>
        <SheetHeader>
          <SheetTitle>Finish review</SheetTitle>
          <SheetClose asChild>
            <UiButton variant="ghost" size="icon" type="button" aria-label="Close review outcome">
              <X size={14} />
            </UiButton>
          </SheetClose>
        </SheetHeader>
        <SheetLede>
          Check what is still open, then choose how to finish.
        </SheetLede>

        <div className="min-h-0 flex-1 overflow-auto px-4 py-4">
          <FinishRecord
            summary={summary}
            record={record}
            verdict={picked ? verdictLabel(picked) : null}
            onOpenStatus={onOpenStatus}
            onOpenPart={onOpenPart}
          />

          <section className="mt-4">
            <div className="review-kicker mb-2">Your verdict</div>
            {currentOutcome?.stale && (
              <div className="mb-2 text-caption leading-4 text-amber-400/80">
                Your previous {currentOutcome.outcome === "lgtm" ? "approval" : currentOutcome.outcome.replaceAll("_", " ")} applies to an older revision. Choose a verdict for this revision.
              </div>
            )}
            <ToggleGroup
              variant="segment"
              aria-label="Overall review outcome"
              className="grid grid-cols-3"
              itemClassName="justify-center text-caption"
              options={OUTCOMES.map((item) => ({ value: item.id, label: item.label, title: item.hint }))}
              value={picked ?? ""}
              onChange={(value) => setOutcome(value as ReviewOutcomeKind)}
            />
            {!picked && (
              <p className="mt-2 text-caption leading-4 text-muted-foreground">
                No verdict chosen. Finishing now records your comments without approving or requesting changes.
              </p>
            )}
            <UiInput
              uiSize="xs"
              type="text"
              value={outcomeSummary}
              onChange={(event) => setOutcomeSummary(event.target.value)}
              aria-label="Review outcome summary"
              placeholder={requestChanges ? "What still needs to change? (optional)" : "Review summary (optional)"}
              className="mt-2"
              maxLength={16_384}
            />
            {requestChanges && (
              <p className="mt-2 text-caption leading-4 text-muted-foreground">
                The Review stays open; the next revision returns here for re-review.
              </p>
            )}
          </section>

          {handoff && (unsent > 0 || requestChanges || toPullRequest) && (
            <section className="mt-4 rounded-md border border-border/80 px-3 py-2" aria-label="Where fixes go">
              <div className="flex items-center gap-2 text-caption">
                <span className="text-muted-foreground">Pending feedback</span>
                <span className="font-mono text-foreground">{plural(unsent)}</span>
                <span aria-hidden="true">→</span>
                <span className="min-w-0 truncate text-foreground">{handoff.label}</span>
                <span className="flex-1" />
                {destinationControl}
              </div>
              {requestChanges ? (
                unsent > 0 ? (
                  <p className={reachable ? "mt-1 text-caption text-muted-foreground" : "mt-1 text-caption text-red-300"}>
                    {reachable
                      ? `Finish will ${deliveryVerb(handoff)} this pending feedback explicitly.`
                      : handoff.reason || "This destination cannot receive feedback."}
                  </p>
                ) : (
                  <p className="mt-1 text-caption text-muted-foreground">
                    {outcomeSummary.trim()
                      ? "No pending comment bundle; the summary is recorded on the Review and goes with the next comments you send."
                      : "Add a comment or a summary for the fixer."}
                  </p>
                )
              ) : offerSend && (
                <label className="mt-2 flex items-center gap-2 text-caption text-subtle">
                  <UiInput
                    unstyled
                    type="checkbox"
                    checked={alsoSend}
                    onChange={(event) => setAlsoSend(event.target.checked)}
                  />
                  Also {deliveryVerb(handoff)} {unsent} to {handoff.label}
                </label>
              )}
            </section>
          )}

          {sendError && (
            <div role="alert" className="mt-4 rounded-md border border-red-900/50 bg-red-950/20 px-3 py-2 text-caption text-red-300">
              Nothing was finished; the comments were not sent: {sendError}
            </div>
          )}
          {finishError && (
            <div role="alert" className="mt-4 rounded-md border border-red-900/50 bg-red-950/20 px-3 py-2 text-caption text-red-300">
              {finishError}
            </div>
          )}
        </div>

        <div className="border-t border-border/80 bg-background/95 px-4 py-3">
          {sendError && (
            <div className="mb-2 flex flex-wrap justify-end gap-2">
              <UiButton
                variant="outline"
                type="button"
                disabled={busy}
                onClick={() => onFinish(outcome, outcomeSummary.trim(), { send: false })}
              >
                Finish without sending
              </UiButton>
              <UiButton
                type="button"
                disabled={busy}
                onClick={() => onFinish(outcome, outcomeSummary.trim(), { send: true })}
              >
                Retry
              </UiButton>
            </div>
          )}
          {!sendError && (
            <div className="flex justify-end gap-2">
              {/* Finishing over open work stays possible but is never the default: then the safe choice leads. */}
              <UiButton variant={finishingAnyway ? "solid" : "ghost"} type="button" disabled={busy} onClick={onClose}>
                Continue reviewing
              </UiButton>
              <UiButton
                variant={finishingAnyway ? "outline" : "solid"}
                type="button"
                disabled={busy || blockedRequest}
                onClick={() => onFinish(outcome, outcomeSummary.trim(), { send: sending })}
              >
                {busy ? "Recording…" : finishLabel}
              </UiButton>
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
