import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, FileText, Footprints, Map as MapIcon, ScanSearch } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { isDialogOpen } from "../../components/ui/dialog";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Hunk, hunkRowVariants } from "../../components/ui/hunk";
import { toastAiReason, useAiGate } from "../aiBudget";
import { classifyFocus, flowAction } from "../flowKeymap";
import { counterFor } from "../focus/focusModel";
import { FLOW_RAIL_CLASS, FlowShortcutsDialog, FlowTopBar, repoNameOf } from "../FlowTopBar";
import { cameFromReview, isColdLoad } from "../reviewBootLocation";
import { skipsOverview, startView } from "../settings/settingsModel";
import { useUserSettings } from "../settings/userSettings";
import { buildReviewFlowPath, type ReviewFlowRoute } from "../reviewRoutes";
import { usePartProofRecord } from "../partProof";
import { queueLine, reviewQueue } from "../queue";
import { hasProofFrom } from "../sinceReview";
import { guardContext } from "../quickCheck/quickCheckModel";
import { usePatchEvidence } from "../quickCheck/patchEvidence";
import { useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import type { Annotation, ReviewLandingReadiness, ReviewOverview, ReviewProjection, ReviewTarget } from "../types";
import { BRIEF_EVIDENCE_HASH, findingCounts, mergeReadinessLine, requiredCheckRows } from "./blockersModel";
import { FindingCountLine, MergeReadinessLine, RequiredCheckRow } from "./ReviewBlockers";
import {
  getDefaultView,
  getEvidenceOpen,
  getPlanApproved,
  planKey,
  setDefaultView,
  setEvidenceOpen,
  setPlanApproved,
  type ReadingView,
} from "./defaultView";
import { BotApprovalHint } from "../github/BotApprovalHint";
import { OverviewEvidence } from "./OverviewEvidence";
import { PolicyEditBanner } from "../policy/PolicyEditBanner";
import { policyEditFiles, usePolicyGate } from "../policy/policyEdit";
import { revisionName } from "../revisionLabel";
import { readLocalSendState } from "../ReviewFlowRoute";
import { SecondStrip } from "../second/SecondStrip";
import { ConfirmAllQuickChecks, type QuickConfirmActions } from "../quickCheck/ConfirmAllQuickChecks";
import { quickConfirmSplit } from "../quickCheck/confirmAllModel";
import { AiBadge, BasedOn, Label, Skeleton } from "./OverviewParts";
import {
  agentName,
  aiHints as hintsFor,
  askedPlanned,
  beforeAfterFor,
  changeTotals,
  checkedRows,
  connectsLine,
  countsText,
  evidenceSections,
  factChip,
  headline,
  hiddenText,
  linesAria,
  linesLabel,
  notChecked,
  notCheckedItems,
  overviewCounts,
  partName,
  partRoute,
  partsOf,
  plural,
  policyLines,
  preparingText,
  questionFor,
  reachModel,
  startRoute,
  testDidNotRun,
  type PreparationSource,
} from "./overviewModel";

export interface ReviewOverviewScreenProps {
  reviewRef: string;
  overview: ReviewOverview;
  /** The review's agent when this revision recorded no host (a later revision often does not), so the name does not flip to "The agent". */
  agentHost?: string;
  targets: readonly ReviewTarget[];
  /** The review's notes: an AI hint that raises a result keeps a part out of the one-step batch here as it does in Quick check. */
  annotations?: readonly Annotation[];
  projection: ReviewProjection | null;
  /** Only the startup slice of the parts is loaded: nothing is counted from it (SPEC 1). */
  partial?: boolean;
  /** The server's own totals for the whole review, usable while the list is partial. */
  partialTotals?: { parts: number | null; files: number | null };
  /** False while the server is still preparing the facts. Start waits for it. */
  ready: boolean;
  progress: PreparationSource | null;
  /** AI help is available: the server has a model for Ask (SPEC 7.1 P2-1 D). */
  aiAvailable: boolean;
  onAsk?: () => void;
  /** The Ask panel, shown beside the Overview while it is open. */
  askPanel?: React.ReactNode;
  /** The review has a walkthrough worth taking (`/walkthrough`); false or absent hides "Walkthrough". */
  tourAvailable?: boolean;
  /** The Review has a plan to brief from (`/brief`). */
  briefAvailable?: boolean;
  /** The change has a Map to walk (`/xray`). */
  xrayAvailable?: boolean;
  /** "Confirm all N quick checks"; absent where a person cannot record answers here (read-only, attached to hosted). */
  quickConfirm?: QuickConfirmActions;
  /** The server's merge readiness for this revision; null until it loads, and on a historical revision. */
  readiness?: ReviewLandingReadiness | null;
}

function Rail({ children }: { children: React.ReactNode }) {
  return <div className={hunkRowVariants({ rail: "p0" })}>{children}</div>;
}

/** How many decisions and quick checks the Overview lists; the rest are one click away in All files (a 10k-part change must not draw 10k rows). */
const OVERVIEW_ROW_CAP = 25;

export function ReviewOverviewScreen(props: ReviewOverviewScreenProps) {
  const { reviewRef, overview, targets, projection, ready, progress, aiAvailable, onAsk } = props;
  const partial = props.partial === true;
  const navigate = useNavigate();
  const ai = useAiGate();
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const { settings, status: settingsStatus } = useUserSettings();
  const [view, setView] = useState<ReadingView>(getDefaultView);
  const viewChosen = useRef(false);
  const [evidenceOpen, setEvidenceOpenState] = useState(getEvidenceOpen);
  const [approvedKey, setApprovedKey] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const wasPreparing = useRef(!ready);

  const proofRecord = usePartProofRecord(overview.session.id, overview.revision.id);
  const counts = useMemo(() => overviewCounts(targets), [targets]);
  const decisions = useMemo(() => partsOf(targets, "decision"), [targets]);
  const quickChecks = useMemo(() => partsOf(targets, "quick_check"), [targets]);
  const asked = useMemo(() => askedPlanned(overview.journal), [overview.journal]);
  const approvalKey = useMemo(() => planKey(reviewRef, overview.revision.id, asked.planned), [reviewRef, overview.revision.id, asked.planned]);
  // Approval is read for the plan on screen: a new revision or a changed plan has another key and reads "Not yet".
  const planApproved = approvedKey === approvalKey || getPlanApproved(approvalKey);
  const hidden = useMemo(() => hiddenText(overview), [overview]);
  const hints = useMemo(() => hintsFor(projection, targets, aiAvailable), [projection, targets, aiAvailable]);
  const reach = useMemo(() => reachModel(projection), [projection]);
  const connects = useMemo(() => connectsLine(projection), [projection]);
  const unchecked = useMemo(() => notCheckedItems(notChecked(projection), reach), [projection, reach]);
  const rows = useMemo(() => checkedRows(targets), [targets]);
  const sections = useMemo(
    () => evidenceSections({ connects: Boolean(connects), reach: Boolean(reach), untrusted: hidden.scanned, notChecked: unchecked.length > 0 }),
    [connects, reach, hidden.scanned, unchecked.length],
  );
  const totals = changeTotals(overview.stats);
  const revision = overview.revision;
  const agentHost = revision.provenance_host || props.agentHost || "";
  const factsIn = ready;
  // P11-2: the order the parts are asked in reads the shared proof record as it stands (no request from here).
  const hasProof = useMemo(() => hasProofFrom(proofRecord), [proofRecord]);
  // The same guard Quick check applies, over the same inputs (the server's answers, AI hints, code that was read and could not be used).
  const evidence = usePatchEvidence(overview.session.id, revision.id);
  const annotations = props.annotations;
  const guards = useMemo(() => guardContext({
    reviewId: overview.session.id, annotationReviewId: overview.session.id, revisionId: revision.id,
    parts: quickChecks, annotations: annotations ?? [], failedPaths: evidence.failed,
  }), [overview.session.id, revision.id, quickChecks, annotations, evidence.failed]);
  const queue = useMemo(() => reviewQueue(targets, revision.id, hasProof, guards), [targets, revision.id, hasProof, guards]);
  const quickSplit = useMemo(() => quickConfirmSplit(quickChecks, guards), [quickChecks, guards]);
  // review-4.1: a later revision leads with what changed since the reviewer's review. Parts they judged on an earlier
  // revision that have not changed since are counted in the queue line and one closing line, not listed again.
  const unchangedIds = useMemo(
    () => new Set(queue.followsReview ? queue.unchanged.map((target) => target.target_id) : []),
    [queue],
  );
  const shownDecisions = useMemo(
    () => (unchangedIds.size ? decisions.filter((target) => !unchangedIds.has(target.target_id)) : decisions),
    [decisions, unchangedIds],
  );
  const shownQuickChecks = useMemo(
    () => (unchangedIds.size ? quickChecks.filter((target) => !unchangedIds.has(target.target_id)) : quickChecks),
    [quickChecks, unchangedIds],
  );

  const open = useCallback((route: ReviewFlowRoute, options?: { wrapUp?: boolean }) => goTo(route, options), [goTo]);
  // Revision 2 onwards, after notes reached the agent: Welcome back (`/rev/<n>`) is the screen built for this moment.
  const humanRevision = overview.revision.revision_number + 1;
  const revisionId = overview.revision.id;
  const [cameBackFor, setCameBackFor] = useState("");
  useEffect(() => {
    if (humanRevision < 2) return;
    let live = true;
    void readLocalSendState(reviewRef, humanRevision).then((state) => {
      // Hidden once this revision has its own verdict; a verdict on an earlier revision (the one that sent the notes) does not count.
      if (live && state.notesSentToAgent && state.verdictRevisionId !== revisionId) setCameBackFor(`${reviewRef}:${humanRevision}`);
    });
    return () => { live = false; };
  }, [reviewRef, humanRevision, revisionId]);
  const cameBack = cameBackFor === `${reviewRef}:${humanRevision}`;
  const openPart = useCallback((target: ReviewTarget) => open(partRoute(target, targets)), [open, targets]);
  const start = useCallback(() => {
    if (ready) open(startRoute(view, targets, hasProof));
  }, [open, ready, targets, view, hasProof]);
  const chooseView = useCallback((next: ReadingView) => {
    viewChosen.current = true;
    setView(next);
    setDefaultView(next);
  }, []);
  const openAllFiles = useCallback(() => {
    chooseView("all");
    open({ kind: "all", part: "" });
  }, [chooseView, open]);
  const openTour = useCallback(() => open({ kind: "walkthrough", step: "", view: "steps" }), [open]);
  const tourAvailable = props.tourAvailable === true && ready;
  const briefAvailable = props.briefAvailable === true && ready;
  const xrayAvailable = props.xrayAvailable === true && ready;
  const openBrief = useCallback(() => open({ kind: "brief" }), [open]);
  const openXRay = useCallback(() => open({ kind: "xray", at: "" }), [open]);
  // Blockers first: a failing required check names no part, so it has its own rows; its evidence is on the Brief.
  const requiredChecks = useMemo(() => requiredCheckRows(projection), [projection]);
  const findings = useMemo(() => findingCounts(projection?.triage), [projection]);
  const readinessLine = mergeReadinessLine(props.readiness);
  const inspectEvidence = briefAvailable ? () => navigate(`${buildReviewFlowPath(reviewRef, { kind: "brief" })}${BRIEF_EVIDENCE_HASH}`) : undefined;

  // Settings › Reading: until the reviewer picks a view here, Start follows the saved default view, and a change over
  // 30 parts opens in All files when that setting is on. The whole parts list must be loaded to count them.
  const partCount = partial ? props.partialTotals?.parts ?? null : targets.length;
  useEffect(() => {
    if (viewChosen.current || settingsStatus === "idle" || settingsStatus === "loading") return;
    setView(startView(settings, partCount ?? 0));
  }, [settings, settingsStatus, partCount]);

  // Settings › Reading "Skip the Overview for reviews with 1 decision": a review opened from outside itself goes
  // straight to its first part (or All files); a move inside the review (Back, the Overview tab) shows the Overview.
  const skipped = useRef(false);
  useEffect(() => {
    if (skipped.current || !ready || partial || settingsStatus === "idle" || settingsStatus === "loading") return;
    skipped.current = true;
    if (cameFromReview(reviewRef) || !skipsOverview(settings, decisions.length)) return;
    goTo(startRoute(startView(settings, targets.length), targets, hasProof), { replace: true });
  }, [ready, partial, settingsStatus, settings, decisions.length, reviewRef, targets, goTo, hasProof]);

  // Live region: the preparing line while it runs, then one "Overview is ready." (SPEC 4.2 Preparing).
  useEffect(() => {
    if (!ready) {
      wasPreparing.current = true;
      setAnnouncement(preparingText(progress));
      return;
    }
    setAnnouncement(wasPreparing.current ? "Overview is ready." : "");
    wasPreparing.current = false;
  }, [ready, progress?.completed, progress?.total, progress?.state]);

  // ⌘K: a review still preparing has no parts yet, so it is a placeholder (R7) and offers only Snooze, Go Home, other reviews and Settings.
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId: overview.session.id,
    parts: ready ? partEntries(reviewRef, targets, "one") : [],
    place: reviewPlace(repoNameOf(overview), overview.title || overview.session.title, "Overview"),
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "overview",
      placeholder: !ready,
      aiAsk: aiAvailable && Boolean(onAsk),
      actions: { start, switchView: openAllFiles, wrapUp: () => open({ kind: "wrap" }, { wrapUp: true }), ask: ai.run(onAsk) },
    },
  });

  // The keys of SPEC 3.5 for this screen, through the keymap: never while typing or while a dialog is open.
  const keys = useRef({ start, onAsk, open, openAllFiles, aiAvailable, aiBlocked: ai.reason });
  keys.current = { start, onAsk, open, openAllFiles, aiAvailable, aiBlocked: ai.reason };
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.defaultPrevented) return;
      const el = document.activeElement as HTMLElement | null;
      const action = flowAction(event, {
        screen: "overview",
        surface: isDialogOpen() || document.querySelector('[role="menu"]') ? "other" : "none",
        focus: el ? classifyFocus({ tagName: el.tagName, isContentEditable: el.isContentEditable, role: el.getAttribute("role") }) : "other",
        aiAsk: keys.current.aiAvailable && Boolean(keys.current.onAsk),
        aiBlocked: keys.current.aiBlocked,
      });
      if (!action) return;
      const now = keys.current;
      switch (action.intent) {
        case "start":
          now.start();
          break;
        case "ask":
          now.onAsk?.();
          break;
        case "aiUnavailable":
          toastAiReason(action.reason ?? now.aiBlocked);
          break;
        case "wrapUp":
          now.open({ kind: "wrap" }, { wrapUp: true });
          break;
        case "help":
          setShortcutsOpen(true);
          break;
        case "switchView":
          // On the Overview G opens All files and remembers the choice, as the prototype does (fulldiff.js).
          now.openAllFiles();
          break;
        default:
          return;
      }
      event.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Focus the heading on every entry but the page's first load (SPEC 5.3, 7.1 P1-3 D).
  const heading = useRef<HTMLHeadingElement>(null);
  const policyFiles = policyEditFiles(overview);
  const policyGate = usePolicyGate(overview.session.id, policyFiles);
  useEffect(() => {
    if (!isColdLoad()) heading.current?.focus({ preventScroll: true });
  }, []);

  const partialParts = props.partialTotals?.parts ?? null;
  const partialFiles = props.partialTotals?.files ?? null;
  const headlineText =
    partialParts !== null && partialFiles !== null
      ? headline(agentHost, { parts: partialParts, files: partialFiles, decisions: 0, quickChecks: 0, acceptedByRule: 0 })
      : null;
  const firstDecision = shownDecisions[0];
  const policies = useMemo(() => policyLines(shownDecisions), [shownDecisions]);
  const title = overview.title || overview.session.title;

  return (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle="Overview"
        title={title}
        revisionNumber={revision.revision_number}
        back={{ text: "Reviews", onClick: () => navigate("/home") }}
        view={{ current: view, onChange: (next) => (next === "all" ? openAllFiles() : chooseView(next)) }}
        counter={counterFor(targets, "", ready && !partial)}
        onAsk={aiAvailable ? onAsk : undefined}
        askKey
        onShortcuts={() => setShortcutsOpen(true)}
        moreItems={[
          { label: "All files", onSelect: openAllFiles },
          { label: "Map", onSelect: () => open({ kind: "map", lens: "data-path", view: "changes" }) },
          { label: "Preview", onSelect: () => open({ kind: "preview", surface: "" }) },
          ...(tourAvailable ? [{ label: "Walkthrough", onSelect: openTour }] : []),
          ...(briefAvailable ? [{ label: "Brief", onSelect: openBrief }] : []),
          ...(xrayAvailable ? [{ label: "X-Ray", onSelect: openXRay }] : []),
        ]}
      />
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={`Enter start · G all files · W wrap up${aiAvailable && onAsk ? " · Q ask" : ""}`} />

      <div className="flex flex-1">
        <div role="region" className="min-w-0 flex-1" data-review-scroll-root="overview" aria-label="Overview">
          <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 px-4 py-6 sm:px-6">
            <div className="flex flex-col gap-2">
              {/* The revision is named once, in the top bar's chip; the body says only that it is the working tree. */}
              {revision.dirty && revision.range_mode !== "commit_range" ? <Label>Working tree</Label> : null}
              <h1 ref={heading} tabIndex={-1} className="text-title font-semibold text-foreground focus:outline-none" data-testid="overview-headline">
                {partial
                  ? headlineText ?? <Skeleton className="h-6 w-3/4" />
                  : headline(agentHost, counts)}
              </h1>
              {cameBack ? (
                <div role="note" className="flex flex-wrap items-center gap-2 rounded-lg border border-p2-line bg-p2-bg px-3 py-2 text-label" data-testid="overview-came-back">
                  <span className="min-w-0 flex-1">{`Your notes came back. ${agentName(agentHost)} made ${revisionName(revision.revision_number)}.`}</span>
                  <Button variant="outline" size="sm" onClick={() => open({ kind: "rev", revision: humanRevision })}>See what changed</Button>
                </div>
              ) : null}
              {ready && !partial && queue.followsReview ? (
                <p className="text-label font-medium text-foreground" data-testid="overview-queue-line">
                  {queueLine(queue)}
                </p>
              ) : null}
              {/* The headline already says what was done; "Did" is only worth saying beside what was asked. */}
              {asked.asked ? (
                <p className="break-words text-label text-muted-foreground" data-testid="overview-asked-did">
                  <strong className="font-semibold text-foreground">Asked:</strong> {asked.asked}
                  {partial && headlineText === null ? null : (
                    <>
                      {" · "}
                      <strong className="font-semibold text-foreground">Did:</strong>{" "}
                      {partial
                        ? `${plural(partialParts ?? 0, "part")} in ${plural(partialFiles ?? 0, "file")}`
                        : `${plural(counts.parts, "part")} in ${plural(counts.files, "file")}`}
                    </>
                  )}
                </p>
              ) : null}
              {findings.length > 0 || readinessLine ? (
                <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1" data-testid="overview-status">
                  <FindingCountLine counts={findings} testId="overview-findings" />
                  <MergeReadinessLine line={readinessLine} testId="overview-merge-readiness" />
                </div>
              ) : null}
            </div>

            <PolicyEditBanner files={policyFiles} gate={policyGate} reviewId={overview.session.id} />

            <BotApprovalHint reviewId={overview.session.id} enabled={Boolean(overview.session.source_ref?.startsWith("github:"))} />

            {!ready ? (
              <p className="text-label text-muted-foreground" data-testid="overview-preparing">
                {preparingText(progress)} · the headline and the task are ready; the rest fills in as facts are checked
              </p>
            ) : null}

            {/* One column at every width: what needs a decision comes first, quick checks and evidence below it. */}
            <div className="flex flex-col gap-4">
            {requiredChecks.length > 0 ? (
              <section aria-label="Required checks" data-testid="overview-required-checks">
                <Hunk>
                  <ul>
                    {requiredChecks.map((row) => (
                      <li key={row.id} className="border-b border-border last:border-b-0">
                        <Rail>
                          <div className="px-3 py-2">
                            <RequiredCheckRow row={row} testId="overview-required-check" onInspectEvidence={inspectEvidence} />
                          </div>
                        </Rail>
                      </li>
                    ))}
                  </ul>
                </Hunk>
              </section>
            ) : null}

            {partial ? (
              <section aria-label="Where to look"><Skeleton className="h-24 w-full" /></section>
            ) : null}

            {!partial && shownDecisions.length > 0 ? (
              <section aria-label="Where to look">
                <Hunk>
                  <Button
                    variant="ghost"
                    className="h-auto w-full justify-between gap-3 rounded-none border-b border-border px-3 py-2 text-left"
                    aria-label={`${plural(shownDecisions.length, "decision")} for you, open the first decision`}
                    data-testid="overview-decisions-open"
                    onClick={() => firstDecision && openPart(firstDecision)}
                  >
                    <span className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <Label tone="red">{plural(shownDecisions.length, "decision")} for you</Label>
                      {factsIn && policies.length > 0 ? (
                        <span className="whitespace-normal text-meta font-normal text-muted-foreground" data-testid="overview-decisions-policy">
                          {policies.length === 1 ? policies[0] : `${policies.length} policies apply: ${policies.join("; ")}`}
                        </span>
                      ) : null}
                    </span>
                    <ChevronRight size={16} aria-hidden="true" className="shrink-0" />
                  </Button>
                  <ul>
                    {shownDecisions.slice(0, OVERVIEW_ROW_CAP).map((target) => {
                      const chip = factsIn ? factChip(target) : null;
                      const question = questionFor(target, projection);
                      return (
                        <li key={target.target_id} className="border-b border-border last:border-b-0">
                          <Rail>
                            <Button
                              variant="ghost"
                              className="h-auto w-full items-start justify-between gap-3 rounded-none px-3 py-2 text-left"
                              data-testid="overview-decision"
                              onClick={() => openPart(target)}
                            >
                              <span className="flex min-w-0 flex-col gap-1">
                                <span className="whitespace-normal break-words font-mono text-label text-foreground">
                                  {partName(target)}
                                  <span className="font-sans text-muted-foreground"> · {target.path}</span>
                                </span>
                                {!factsIn ? <Skeleton className="w-2/3" /> : question ? <span className="whitespace-normal text-label font-normal text-foreground">{question}</span> : null}
                              </span>
                              {!factsIn ? <Skeleton className="h-5 w-16 shrink-0" /> : chip ? <Badge tone={chip.tone} className={chip.plain ? "shrink-0 whitespace-normal font-sans normal-case" : "shrink-0"} title={chip.policy}>{chip.label}</Badge> : null}
                            </Button>
                          </Rail>
                        </li>
                      );
                    })}
                    {shownDecisions.length > OVERVIEW_ROW_CAP ? (
                      <li className="px-3 py-2 text-label text-muted-foreground" data-testid="overview-decisions-more">
                        <Button variant="ghost" size="sm" onClick={openAllFiles}>{plural(shownDecisions.length - OVERVIEW_ROW_CAP, "more decision", "more decisions")} · open All files</Button>
                      </li>
                    ) : null}
                  </ul>
                </Hunk>
              </section>
            ) : null}

            <div className="flex min-w-0 flex-col gap-4">
            {!partial ? <SecondStrip apiReviewId={overview.session.id} /> : null}

            {!partial && shownQuickChecks.length > 0 ? (
              <section aria-label="Quick checks" className="rounded-lg border border-border bg-card" data-testid="overview-quick">
                <div className="flex items-center justify-between gap-2 px-3 py-2">
                  <span className="flex min-w-0 flex-wrap items-baseline gap-x-2">
                    <Label>{plural(shownQuickChecks.length, "quick check")}</Label>
                    <span className="text-meta text-muted-foreground">Glance and confirm</span>
                  </span>
                  {props.quickConfirm && ready ? (
                    <span className="ml-auto">
                      <ConfirmAllQuickChecks parts={quickSplit.confirm} ownAnswer={quickSplit.ownAnswer} apiReviewId={overview.session.id} sessionId={overview.session.id} revisionId={revision.id} actions={props.quickConfirm} />
                    </span>
                  ) : null}
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Open the quick check"
                   
                    onClick={() => openPart(shownQuickChecks[0])}
                  >
                    <ChevronRight size={16} aria-hidden="true" />
                  </Button>
                </div>
                <ul>
                  {shownQuickChecks.slice(0, OVERVIEW_ROW_CAP).map((target) => {
                    const line = beforeAfterFor(target, projection, aiAvailable);
                    return (
                      <li key={target.target_id} className="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-border px-3 py-2 text-label" data-testid="overview-quick-row">
                        <span className="font-mono text-foreground">{partName(target)}</span>
                        {line ? (
                          <span className="flex flex-wrap items-center gap-x-2 gap-y-1" data-testid="overview-before-after">
                            <AiBadge />
                            <span>{line.before}</span>
                            <span aria-hidden="true">→</span>
                            <strong className="font-semibold text-foreground">{line.after}</strong>
                            <BasedOn label={linesLabel(target)} aria={linesAria(target)} onOpen={() => openPart(target)} />
                          </span>
                        ) : (
                          <span className="text-muted-foreground">1 part changed in {target.path}</span>
                        )}
                        {factsIn && testDidNotRun(target) ? <Badge tone="warning">Test did not run</Badge> : null}
                      </li>
                    );
                  })}
                  {shownQuickChecks.length > OVERVIEW_ROW_CAP ? (
                    <li className="border-t border-border px-3 py-2 text-label text-muted-foreground" data-testid="overview-quick-more">
                      <Button variant="ghost" size="sm" onClick={openAllFiles}>{plural(shownQuickChecks.length - OVERVIEW_ROW_CAP, "more quick check", "more quick checks")} · open All files</Button>
                    </li>
                  ) : null}
                </ul>
              </section>
            ) : null}

            {ready && !partial && unchangedIds.size > 0 ? (
              <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-label text-muted-foreground" data-testid="overview-unchanged">
                <span>{`${plural(unchangedIds.size, "part")} you reviewed did not change, so ${unchangedIds.size === 1 ? "it is" : "they are"} not listed here.`}</span>
                <Button variant="ghost" size="sm" onClick={openAllFiles}>See every part in All files</Button>
              </p>
            ) : null}

            <OverviewEvidence
              sections={sections}
              checking={!ready}
              open={evidenceOpen}
              onToggle={() => {
                setEvidenceOpenState((value) => {
                  setEvidenceOpen(!value);
                  return !value;
                });
              }}
              agent={agentName(agentHost)}
              counts={counts}
              totals={totals}
              asked={asked}
              planApproved={planApproved}
              onTogglePlan={() => {
                const next = !planApproved;
                setPlanApproved(approvalKey, next);
                setApprovedKey(next ? approvalKey : "");
              }}
              connects={connects}
              onOpenMap={() => open({ kind: "map", lens: "data-path", view: "changes" })}
              checkedRows={rows}
              aiAvailable={aiAvailable}
              aiHints={hints}
              onOpenPart={openPart}
              aiBlocked={ai.reason}
              onOpenSettings={() => navigate("/settings/review#ai-help")}
              reach={reach}
              hidden={hidden}
              notChecked={unchecked}
            />
            </div>
            </div>
          </div>
        </div>
        {props.askPanel ? (
          <aside data-review-rail="ask" className={`review-secondary-rail ${FLOW_RAIL_CLASS} w-[min(32vw,400px)] shrink-0 border-l border-border bg-background`}>
            {props.askPanel}
          </aside>
        ) : null}
      </div>

      <footer data-bar="start" className="sticky bottom-0 z-20 flex h-16 shrink-0 items-center justify-between gap-3 border-t border-border bg-background px-[max(1rem,calc((100%-72rem)/2+1.5rem))]">
        <span id="overview-bar-text" className="min-w-0 truncate text-label text-muted-foreground" data-testid="overview-bar-text">
          {ready ? (
            <>
              <strong className="font-semibold text-foreground">{countsText({ decisions: shownDecisions.length, quickChecks: shownQuickChecks.length })}</strong>
            </>
          ) : (
            "Preparing · you can read the headline now"
          )}
        </span>
        <span className="flex shrink-0 items-center gap-2">
          {tourAvailable ? (
            <Button variant="ghost" size="sm" className="hidden md:inline-flex" data-testid="overview-open-tour" onClick={openTour}>
              <Footprints size={16} aria-hidden="true"/> Walkthrough
            </Button>
          ) : null}
          {briefAvailable ? (
            <Button variant="ghost" size="sm" className="hidden md:inline-flex" data-testid="overview-open-brief" onClick={openBrief}>
              <FileText size={16} aria-hidden="true"/> Brief
            </Button>
          ) : null}
          {xrayAvailable ? (
            <Button variant="ghost" size="sm" className="hidden md:inline-flex" data-testid="overview-open-xray" onClick={openXRay}>
              <ScanSearch size={16} aria-hidden="true"/> X-Ray
            </Button>
          ) : null}
          <Button variant="ghost" size="sm" className="hidden md:inline-flex" data-testid="overview-open-map" onClick={() => open({ kind: "map", lens: "data-path", view: "changes" })}>
            <MapIcon size={16} aria-hidden="true"/> Map
          </Button>
          <Button variant="ghost" size="sm" className="hidden md:inline-flex" data-testid="overview-open-preview" onClick={() => open({ kind: "preview", surface: "" })}>
            Preview
          </Button>
          <Button variant="ghost" size="sm" className="hidden md:inline-flex" data-testid="overview-all-files" onClick={openAllFiles}>
            All files
          </Button>
          <Button
            variant="brand"
            size="sm"
            className="h-10 px-4"
            disabled={!ready}
            aria-describedby={ready ? undefined : "overview-bar-text"}
            data-testid="overview-start"
            onClick={start}
          >
            Start review
            <span aria-hidden="true" className="hidden rounded-sm border border-current px-1 font-mono text-tag md:inline">Enter</span>
          </Button>
        </span>
      </footer>

      <div role="status" aria-live="polite" className="sr-only" data-testid="overview-live">
        {announcement}
      </div>
    </>
  );
}
