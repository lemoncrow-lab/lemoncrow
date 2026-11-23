import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Clock3, Flag, Link2, LoaderCircle, Play, Sparkles, X } from "lucide-react";
import type { CodeTarget } from "../../lib/codeLinks";
import type { InvestigateHost } from "../investigate/Investigate";

import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Checkbox } from "../../components/ui/checkbox";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Sheet, SheetBody, SheetClose, SheetContent, SheetTitle } from "../../components/ui/sheet";
import { Textarea } from "../../components/ui/textarea";
import { FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf } from "../FlowTopBar";
import { toastAiReason, useAiGate } from "../aiBudget";
import { classifyFocus, flowAction, focusLegend } from "../flowKeymap";
import { isDialogOpen } from "../../components/ui/dialog";
import { fetchTargetHistory } from "../reviewApi";
import { fetchCheckRecipes, type CheckRecipesProjection } from "../checkRecipesApi";
import { runPartTest as runSharedPartTest, usePartTestRun } from "../partTestRuns";
import { usePartProof, usePartProofRecord } from "../partProof";
import { hasProofFrom, readOrder } from "../sinceReview";
import { PartProofBadge, PartProofNote } from "../PartProofBadge";
import { isColdLoad } from "../reviewBootLocation";
import { useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { useCopyLink, useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import { buildReviewFlowPath } from "../reviewRoutes";
import { focusResume } from "../home/resumeFor";
import { useSaveResume } from "../home/useSaveResume";
import { agentName, factChip, linesLabel, partRoute, partSlug, partsOf, testDidNotRun } from "../overview/overviewModel";
import { setDefaultView } from "../overview/defaultView";
import { getFlowBindings, useFlowBindings } from "../settings/userSettings";
import type { Annotation, AnnotationState, MarkState, ReviewChangeProposal, ReviewMarkEvent, ReviewOverview, ReviewProjection, ReviewTarget } from "../types";
import { FocusCode, scrollToCodeLine, type LineComment } from "./FocusCode";
import { CodeViewControls } from "../codeView";
import { useSeenCoverage } from "../seen/SeenCoverageProvider";
import ReviewPartPreviewDisclosure from "../previewFlow/ReviewPartPreviewDisclosure";
import { FocusStructurePanel } from "./FocusStructurePanel";
import { aiFindingsForPart, aiHintRanges, inlineNotesForPart, notesForPart, visibleReviewNotes } from "./focusAnnotations";
import { loadFocusDraft, saveFocusDraft } from "./focusDraftPersistence";
import {
  answerBadge, confirmationText, confirmationTone, mergeRanges, counterFor, decisionQueue, interstitialHeading, momentumText, movedToEndOnce,
  LATER_OFF_REASON, MOVED_ONCE_REASON, nextNeeding, notableSites, partName, partPosition, pauseHeading, pauseReason,
  onlyCheckThereLine, planDecideLater, planLooksGood, questionFor, resolvePart, stepPart, type Confirmation,
} from "./focusModel";
import { policyEditFiles, policyPartBlock, policyQuestion, usePolicyStatus } from "../policy/policyEdit";
import { PolicyPartAsk } from "../policy/PolicyPartAsk";
import { MovedFromLabel } from "../MovedFromLabel";
import { revealBriefDecision } from "../investigate/revealDecision";
import { PHONE_DOCK_BUTTON, answerPressed, pressedIn } from "../answerTone";

/** A side sheet's title row with its own Close button (Esc and the overlay close it too). */
function SheetHead({ title }: { title: string }) {
  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-border px-4 py-2">
      <SheetTitle className="min-w-0 flex-1 text-heading font-medium">{title}</SheetTitle>
      <SheetClose asChild>
        <Button variant="ghost" size="icon" className="shrink-0" aria-label="Close"><X size={16} aria-hidden="true" /></Button>
      </SheetClose>
    </div>
  );
}

// X-Ray, Assumptions, What if, Blind spots and Challenge load only when the reviewer opens Investigate.
const Investigate = lazy(() => import("../investigate/Investigate"));

/** What the Reader holds for Investigate; the Focus screen supplies the navigation. */
export type FocusInvestigateHost = Omit<InvestigateHost, "onWalk" | "onOpenTarget" | "onOpenCodeLocation" | "onOpenDecision">;

export interface ReviewFocusScreenProps {
  reviewRef: string;
  /** Canonical API ID; the URL can contain a distinct public review reference. */
  apiReviewId: string;
  part: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  annotations?: readonly Annotation[];
  proposals?: readonly ReviewChangeProposal[];
  projection: ReviewProjection | null;
  complete: boolean;
  aiAvailable: boolean;
  readOnly: boolean;
  onMark: (target: ReviewTarget, state: MarkState) => Promise<boolean | "queued">;
  /** A whole-part note, or (with `line`) an inline comment on a line or range written in the code. */
  onNote: (target: ReviewTarget, body: string, reconsideredAfterStructure: boolean, line?: LineComment) => Promise<boolean | "queued">;
  onAnnotationState?: (id: string, state: AnnotationState) => void;
  /** "Ask about this part" (the card). */
  onAsk?: (target: ReviewTarget) => void;
  /** "Ask about the change" (the top bar). */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
  /** Investigate this part (X-Ray, assumptions, what if, blind spots, challenge); absent where the Review has no Brief capability. */
  investigate?: FocusInvestigateHost;
}

const CONFIRMATION_CLASS = { success: "border-ok-line bg-ok-bg", warning: "border-p1-line bg-p1-bg", neutral: "border-border" } as const;

/** One decision at a time, backed by the Reader's existing store and write pipeline. */
export function ReviewFocusScreen(props: ReviewFocusScreenProps) {
  const { reviewRef, apiReviewId, overview, targets, projection, aiAvailable, readOnly, onMark, onNote, onAsk, onAskChange } = props;
  const canWrite = !readOnly && props.complete;
  const ai = useAiGate();
  const bindings = useFlowBindings();
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const seen = useSeenCoverage();
  // P11-2: parts changed since the reviewer's review come first, no proof before proof; read from the shared record as it stands.
  const proofRecord = usePartProofRecord(apiReviewId, overview.revision.id);
  const hasProof = useMemo(() => hasProofFrom(proofRecord), [proofRecord]);
  const queue = useMemo(() => decisionQueue(targets, hasProof), [targets, hasProof]);
  const quick = useMemo(() => readOrder(partsOf(targets, "quick_check"), hasProof), [targets, hasProof]);
  const current = resolvePart(targets, props.part) ?? (props.part ? null : queue.find((target) => target.state !== "reviewed" && target.state !== "needs_changes") ?? quick.find((target) => target.state !== "reviewed" && target.state !== "needs_changes") ?? queue[0] ?? quick[0] ?? null);
  const isDecision = Boolean(current && queue.some((target) => target.target_id === current.target_id));
  const counter = counterFor(targets, current?.target_id ?? "", props.complete, hasProof);
  useSaveResume(apiReviewId, canWrite ? focusResume(targets, current, queue) : null, canWrite && Boolean(current));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [interstitial, setInterstitial] = useState(false);
  const [structureOpen, setStructureOpen] = useState(false);
  const [investigateOpen, setInvestigateOpen] = useState(false);
  const [wholeOpen, setWholeOpen] = useState(false);
  const [composerOpen, setComposerOpen] = useState(false);
  const [includedFix, setIncludedFix] = useState<{ key: string; id: string } | null>(null);
  // A kept draft belongs to exactly one part on one frozen revision. Never attach it to the next part.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [keptDrafts, setKeptDrafts] = useState<ReadonlySet<string>>(new Set());
  const [notesSaved, setNotesSaved] = useState(0);
  const [changedAfterStructure, setChangedAfterStructure] = useState(false);
  const [pauseTarget, setPauseTarget] = useState("");
  const [phonePauseExpanded, setPhonePauseExpanded] = useState(false);
  const [history, setHistory] = useState<{ key: string; events: ReviewMarkEvent[]; ok: boolean } | null>(null);
  const [checks, setChecks] = useState<CheckRecipesProjection | null>(null);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const testResultRef = useRef<HTMLDivElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const mobileKeepRef = useRef<HTMLButtonElement>(null);
  const interstitialHeadingRef = useRef<HTMLHeadingElement>(null);
  const key = current ? `${overview.revision.id}\u0000${current.unit_key}` : "";
  const persistedDraft = useMemo(() => current ? loadFocusDraft(apiReviewId, overview.revision.id, current.unit_key) : "", [apiReviewId, overview.revision.id, current?.unit_key]);
  const draft = drafts[key] ?? persistedDraft;
  const draftKept = keptDrafts.has(key) || (drafts[key] === undefined && Boolean(persistedDraft));
  const setDraft = (value: string) => {
    if (current) saveFocusDraft(apiReviewId, overview.revision.id, current.unit_key, value);
    setDrafts((before) => ({ ...before, [key]: value }));
  };
  const visibleNotes = useMemo(() => visibleReviewNotes(props.annotations ?? [], overview.session.id, overview.revision.id), [props.annotations, overview.session.id, overview.revision.id]);
  const partNotes = useMemo(() => notesForPart(visibleNotes, current), [visibleNotes, current]);
  const inlineNotes = useMemo(() => inlineNotesForPart(props.annotations ?? [], overview.session.id, overview.revision.id, current), [props.annotations, overview.session.id, overview.revision.id, current]);
  const aiFindings = useMemo(() => aiFindingsForPart(props.annotations ?? [], overview.session.id, overview.revision.id, current, aiAvailable), [props.annotations, overview.session.id, overview.revision.id, current, aiAvailable]);
  const hintRanges = useMemo(() => current ? mergeRanges(aiHintRanges(aiFindings, current)) : [], [current, aiFindings]);
  const hintLine = useMemo(() => current ? onlyCheckThereLine({ start: current.start_line, end: current.end_line }, aiHintRanges(aiFindings, current), aiAvailable) : null, [current, aiFindings, aiAvailable]);
  const aiFix = aiAvailable && current ? props.proposals?.find((proposal) =>
    proposal.base_revision_id === overview.revision.id && proposal.target_unit_key === current.unit_key && proposal.path === current.path &&
    proposal.state === "proposed" && proposal.patch.length > 0 && proposal.patch.length <= 8192 &&
    aiFindings.some((finding) => finding.id === proposal.annotation_id),
  ) : undefined;
  const [notesOpen, setNotesOpen] = useState(false);
  const sites = useMemo(() => current ? notableSites(current, projection) : [], [current, projection]);
  const isPaused = Boolean(current && pauseTarget === current.target_id);
  const next = current && isDecision ? nextNeeding(queue, current.target_id) : null;
  const nextQuick = quick.find((target) => target.state !== "reviewed" && target.state !== "needs_changes") ?? null;
  const hadMoved = current?.state === "later" || (history?.key === key && history.ok && current
    ? movedToEndOnce(current, history.events, overview.revision.id) : false);
  const historyReady = current?.state === "later" || (history?.key === key && history.ok);
  // Decide later stays focusable and says why (aria-disabled), instead of dropping out of the tab order.
  const laterOn = overview.later_marks !== false;
  const laterReason = !isDecision ? "" : !laterOn ? LATER_OFF_REASON : hadMoved ? MOVED_ONCE_REASON : !historyReady ? "History not verified" : "";
  // SPEC 4.19 (POL-1): a policy file's own part cannot be answered by someone who may not approve it.
  const policyFiles = policyEditFiles(overview);
  const policyStatus = usePolicyStatus(apiReviewId, policyFiles);
  const policyBlock = current ? policyPartBlock(current.path, policyFiles, policyStatus?.gate ?? null) : "";
  const mapping = checks?.revision_id === overview.revision.id && current
    ? checks.part_checks?.find((item) => item.unit_key === current.unit_key && item.path === current.path && item.symbol === current.symbol)
    : undefined;
  const runState = usePartTestRun(apiReviewId, overview.revision.id, current?.unit_key ?? "") ?? null;
  // P11-1: the revision's one proof record (the parts in view run first); the same words as Quick check and All files.
  const proof = usePartProof(apiReviewId, overview.revision.id, { inView: current ? [current.path] : [], autoRun: canWrite });
  // A background/CI result is not the reviewer's requested Run test (D8).
  // Only this card's explicit run changes the per-part test chip.
  const reportedVerdict = runState?.state === "done" ? runState.verdict : undefined;

  useEffect(() => {
    let live = true;
    setChecks(null);
    fetchCheckRecipes(apiReviewId, overview.revision.id).then((result) => {
      if (live && result.revision_id === overview.revision.id) setChecks(result);
    }).catch(() => { /* No trusted mapping means no Run test, never a guessed one. */ });
    return () => { live = false; };
  }, [apiReviewId, overview.revision.id]);

  useEffect(() => {
    if (runState?.state === "done" || runState?.state === "error") testResultRef.current?.focus({ preventScroll: true });
  }, [runState?.state, runState?.verdict]);

  // /one is a valid address in its own right (NAV-2); show the first open part without rewriting the URL.
  // Answering or clicking a pip can navigate to /one/<part> while a cold reload keeps /one intact.

  useEffect(() => {
    if (!current) return;
    let live = true;
    setHistory(null);
    fetchTargetHistory(apiReviewId, current.unit_key).then((result) => {
      if (live) setHistory({ key, events: result.events, ok: true });
    }).catch(() => {
      if (live) setHistory({ key, events: [], ok: false });
    });
    return () => { live = false; };
  }, [apiReviewId, key, current?.unit_key]);

  useEffect(() => {
    setError("");
    setCopyStatus("");
    setWholeOpen(false);
    setPauseTarget("");
    setPhonePauseExpanded(false);
    setChangedAfterStructure(false);
    setInterstitial(false);
    setNotesOpen(false);
    setIncludedFix(null);
    if (!isColdLoad()) headingRef.current?.focus({ preventScroll: true });
  }, [current?.target_id, overview.revision.id]);

  useEffect(() => {
    if (interstitial) interstitialHeadingRef.current?.focus({ preventScroll: true });
  }, [interstitial]);

  useEffect(() => {
    if (!isPaused) return;
    const phone = window.matchMedia?.("(max-width: 639px)").matches;
    (phone ? mobileKeepRef : keepRef).current?.focus({ preventScroll: true });
  }, [isPaused]);

  const visit = useCallback((target: ReviewTarget) => {
    setInterstitial(false);
    setConfirmation(null);
    goTo(partRoute(target, targets));
  }, [goTo, targets]);

  const advance = useCallback((from: ReviewTarget, kind: Confirmation["kind"]) => {
    const following = nextNeeding(queue, from.target_id);
    const unjudgedQuick = quick.find((item) => item.target_id !== from.target_id && item.state !== "reviewed" && item.state !== "needs_changes");
    if (!isDecision) {
      // Quick checks complete into Wrap, not back into the risk queue or its interstitial.
      setConfirmation(null);
      if (unjudgedQuick) goTo(partRoute(unjudgedQuick, targets));
      else goTo({ kind: "wrap" }, { wrapUp: true });
    } else if (following) {
      setConfirmation({ kind, part: from.target_id, next: partName(following) });
      goTo(partRoute(following, targets));
    } else if (kind === "later") {
      // `later` is still unanswered. With no other open part, remain on this one rather than claim all judged.
      setConfirmation(null);
      setInterstitial(false);
    } else {
      setConfirmation(null);
      setInterstitial(true);
    }
  }, [goTo, isDecision, queue, quick, targets]);

  const mark = useCallback(async (state: "reviewed" | "later" | "unreviewed") => {
    if (!current || pending || !canWrite) return;
    if (state === "later" && (!isDecision || !laterOn || !historyReady || hadMoved)) return;
    const plan = state === "reviewed" ? planLooksGood(current, sites.length > 0) : state === "later" ? planDecideLater(hadMoved) : null;
    if (plan?.blocked) return;
    setPending(true);
    setError("");
    try {
      const ok = plan?.mark === null ? true : await onMark(current, state);
      if (ok === "queued") {
        setError("The answer was queued for hosted review. It has not been applied yet, so this part stays open.");
        return;
      }
      if (!ok) {
        setError("The answer was not saved. This part has not advanced.");
        return;
      }
      if (state === "reviewed" && isDecision) seen.focusAnswered(current);
      if (state === "unreviewed") {
        setPauseTarget("");
        setChangedAfterStructure(true);
      } else if (plan?.pause && state === "reviewed") {
        // The spec records green, then pauses once to let the reviewer reconsider.
        setPauseTarget(current.target_id);
        setConfirmation(null);
      } else {
        setPauseTarget("");
        advance(current, state === "later" ? "later" : "good");
      }
    } finally {
      setPending(false);
    }
  }, [current, pending, canWrite, isDecision, laterOn, historyReady, hadMoved, sites.length, onMark, advance, seen.focusAnswered]);

  const keepAnswer = useCallback(() => {
    if (!current || !isPaused || pending) return;
    setPauseTarget("");
    advance(current, "good");
  }, [current, isPaused, pending, advance]);

  const saveNote = useCallback(async () => {
    if (!current || pending || !canWrite || !draft.trim()) return;
    setPending(true);
    setError("");
    try {
      const actualBody = includedFix?.key === key && includedFix.id === aiFix?.id
        ? `${draft.trim()}\n\nAI suggested fix (unverified; explicitly included by reviewer):\n${aiFix.patch}`
        : draft.trim();
      const ok = await onNote(current, actualBody, changedAfterStructure);
      if (ok === "queued") {
        // Hosted accepted this note into its queue, so retrying it locally risks a duplicate.
        saveFocusDraft(apiReviewId, overview.revision.id, current.unit_key, "");
        setDrafts((before) => ({ ...before, [key]: "" }));
        setKeptDrafts((before) => { const next = new Set(before); next.delete(key); return next; });
        setComposerOpen(false);
        setError("The note was queued for hosted review. Wait for it to be applied before deciding.");
        return;
      }
      if (!ok) {
        setError("The note was not saved. Keep the draft and retry.");
        return;
      }
      if (isDecision) seen.focusAnswered(current);
      setNotesSaved((count) => count + 1);
      saveFocusDraft(apiReviewId, overview.revision.id, current.unit_key, "");
      setDrafts((before) => ({ ...before, [key]: "" }));
      setKeptDrafts((before) => { const next = new Set(before); next.delete(key); return next; });
      setComposerOpen(false);
      setChangedAfterStructure(false);
      advance(current, "change");
    } finally {
      setPending(false);
    }
  }, [apiReviewId, overview.revision.id, current, pending, canWrite, key, draft, changedAfterStructure, onNote, advance, includedFix, aiFix, isDecision, seen.focusAnswered]);

  const commentOnLine = useCallback(async (line: LineComment) => {
    if (!current || !canWrite) return false;
    const ok = await onNote(current, line.body, changedAfterStructure, line);
    if (ok === true && !line.parentId) setNotesSaved((count) => count + 1);
    return ok;
  }, [current, canWrite, onNote, changedAfterStructure]);

  const closeComposer = useCallback(() => {
    setComposerOpen(false);
    if (draft.trim()) setKeptDrafts((before) => new Set(before).add(key));
  }, [draft, key]);
  const runPartTest = useCallback(async () => {
    if (!mapping || !current || !canWrite) return;
    // The shared store sends `fresh: true` and reads the verdict from this run's own finished report.
    await runSharedPartTest({ reviewId: apiReviewId, revisionId: overview.revision.id, unitKey: current.unit_key, mapping, path: current.path });
  }, [mapping, current, canWrite, apiReviewId, overview.revision.id]);
  const goToLine = useCallback((line: number) => {
    const code = document.querySelector<HTMLElement>('[data-testid="focus-card"] [data-testid="focus-code"]');
    if (!scrollToCodeLine(code, line)) code?.scrollIntoView?.({ block: "center" });
    code?.focus({ preventScroll: true });
  }, []);
  const switchToAll = useCallback(() => {
    setDefaultView("all");
    goTo({ kind: "all", part: current ? partSlug(current, targets) : "" });
  }, [goTo, current, targets]);
  const ask = useCallback(() => { if (current) onAsk?.(current); }, [current, onAsk]);
  // Investigate's links land in the flow: another part opens in Focus, this part scrolls to the line.
  const openFromInvestigate = useCallback((part: ReviewTarget | undefined, line: number) => {
    setInvestigateOpen(false);
    if (!part) return;
    if (part.target_id !== current?.target_id) visit(part);
    else window.requestAnimationFrame(() => goToLine(line));
  }, [current?.target_id, visit, goToLine]);
  const investigateTarget = useCallback((targetId: string) => {
    const part = targets.find((item) => item.target_id === targetId);
    openFromInvestigate(part, part?.start_line ?? 0);
  }, [targets, openFromInvestigate]);
  const investigateCode = useCallback((location: CodeTarget) => {
    const samePath = targets.filter((item) => item.path === location.path);
    const part = samePath.find((item) => item.start_line <= location.line && location.line <= item.end_line) ?? samePath[0];
    openFromInvestigate(part, location.line);
  }, [targets, openFromInvestigate]);
  const copyWithToast = useCopyLink();
  const copyPartLink = useCallback(async () => {
    if (!current) return;
    const href = new URL(buildReviewFlowPath(reviewRef, partRoute(current, targets)), window.location.href).href;
    // With the palette mounted the copy says "Link copied" in its toast (SPEC 4.12); a bare screen keeps its own line.
    if (copyWithToast) { await copyWithToast(href); return; }
    try {
      await navigator.clipboard.writeText(href);
      setCopyStatus("Link copied to this part");
    } catch {
      setCopyStatus("Could not copy the link. Use the address bar instead.");
    }
  }, [current, reviewRef, targets, copyWithToast]);
  // One answer for the A key and the palette: on a part already Looks good it keeps the answer and moves on.
  const looksGood = () => {
    if (current?.state === "reviewed") {
      if (isPaused) void keepAnswer(); else advance(current, "good");
    } else void mark("reviewed");
  };
  const currentActions = useRef({ looksGood, switchToAll, mark, keepAnswer, saveNote, closeComposer, ask, runPartTest, copyPartLink, visit, setStructureOpen, setWholeOpen, goTo, nextUnseen: seen.nextUnseen, current, next, queue, isPaused, interstitial, structureOpen, aiAvailable, onAsk, aiBlocked: ai.reason, historyReady, testAvailable: Boolean(mapping && canWrite) });
  currentActions.current = { looksGood, switchToAll, mark, keepAnswer, saveNote, closeComposer, ask, runPartTest, copyPartLink, visit, setStructureOpen, setWholeOpen, goTo, nextUnseen: seen.nextUnseen, current, next, queue, isPaused, interstitial, structureOpen, aiAvailable, onAsk, aiBlocked: ai.reason, historyReady, testAvailable: Boolean(mapping && canWrite && runState?.state !== "running") };

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.defaultPrevented) return;
      const el = document.activeElement as HTMLElement | null;
      const action = flowAction(event, {
        screen: "one",
        bindings: getFlowBindings(),
        surface: isDialogOpen() || document.querySelector('[role="menu"]') ? (currentActions.current.current && document.querySelector('[data-focus-composer]') ? "composer" : "other") : "none",
        focus: el ? classifyFocus({ tagName: el.tagName, isContentEditable: el.isContentEditable, role: el.getAttribute("role") }) : "other",
        aiAsk: currentActions.current.aiAvailable && Boolean(currentActions.current.onAsk),
        aiBlocked: currentActions.current.aiBlocked,
        testAvailable: currentActions.current.testAvailable,
        isDecision: Boolean(currentActions.current.current && currentActions.current.queue.some((part) => part.target_id === currentActions.current.current?.target_id)),
        answeredGood: currentActions.current.current?.state === "reviewed",
        answered: currentActions.current.current?.state === "reviewed" || currentActions.current.current?.state === "needs_changes",
        pauseStrip: currentActions.current.isPaused,
        interstitial: currentActions.current.interstitial,
      });
      if (!action) return;
      const callbacks = currentActions.current;
      switch (action.intent) {
        case "looksGood": callbacks.looksGood(); break;
        case "decideLater": void callbacks.mark("later"); break;
        case "needsChange": setComposerOpen(true); break;
        case "structure": setStructureOpen(true); break;
        case "showWhole": setWholeOpen((open) => !open); break;
        case "ask": callbacks.ask(); break;
        case "aiUnavailable": toastAiReason(action.reason ?? callbacks.aiBlocked); break;
        case "runTest": void callbacks.runPartTest(); break;
        case "copyLink": void callbacks.copyPartLink(); break;
        case "keepAnswer": void callbacks.keepAnswer(); break;
        case "previousPart": {
          const before = callbacks.current && stepPart(callbacks.queue, callbacks.current.target_id, -1);
          if (before) callbacks.visit(before);
          break;
        }
        case "nextPart":
          if (callbacks.next) callbacks.visit(callbacks.next);
          else if (callbacks.current && (callbacks.current.state === "reviewed" || callbacks.current.state === "needs_changes")) setInterstitial(true);
          break;
        case "goQuickCheck": if (quick[0]) callbacks.visit(quick[0]); break;
        case "nextUnseenRisky": callbacks.nextUnseen(); break;
        case "wrapUp": callbacks.goTo({ kind: "wrap" }, { wrapUp: true }); break;
        case "switchView": callbacks.switchToAll(); break;
        case "help": setShortcutsOpen(true); break;
        case "saveNote": void callbacks.saveNote(); break;
        case "closeSurface":
          if (action.surface === "composer") callbacks.closeComposer();
          else if (callbacks.structureOpen) callbacks.setStructureOpen(false);
          else return; // Let another owner (such as Ask) handle its own Escape.
          break;
        default: return;
      }
      event.preventDefault();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [advance, quick]);

  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, interstitial || !current ? "One at a time" : partName(current)),
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "one",
      part: current && !interstitial ? partName(current) : undefined,
      canAnswer: canWrite && !pending && !isPaused && Boolean(current) && !interstitial && !policyBlock,
      isDecision: isDecision && !laterReason,
      structureAvailable: Boolean(current) && !interstitial,
      aiAsk: aiAvailable && Boolean(onAsk) && Boolean(current) && !interstitial,
      view: "one",
      actions: {
        looksGood,
        needsChange: () => setComposerOpen(true),
        decideLater: () => void mark("later"),
        structure: () => setStructureOpen(true),
        switchView: switchToAll,
        wrapUp: () => goTo({ kind: "wrap" }, { wrapUp: true }),
        copyLink: () => void copyPartLink(),
        ask: ai.run(ask),
      },
    },
  });

  const sourceCount = new Set(targets.map((target) => target.path)).size;
  const decisions = queue;
  const position = current ? partPosition(targets, current.target_id, hasProof) : { n: 0, total: 0 };
  const before = current && isDecision ? stepPart(queue, current.target_id, -1) : null;
  const focusName = current ? partName(current) : "";
  const fact = current ? factChip(current) : null;
  const question = current ? (policyQuestion(policyStatus, current.path) || questionFor(current, projection)) : "";
  const fromWrap = new URLSearchParams(window.location.search).get("from") === "wrap";
  const anchor = current ? `${current.path}${current.start_line > 0 ? ` · ${current.end_line > current.start_line ? `lines ${current.start_line}–${current.end_line}` : `line ${current.start_line}`}` : ""} · ${focusName}` : "";
  const agentHost = overview.revision.provenance_host ?? "";
  const agent = agentHost.trim() ? agentName(agentHost) : "the agent";
  const hosted = Boolean(overview.session.hosted);

  return (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle={interstitial ? "Risky parts judged" : `Focus: ${focusName}`}
        title={overview.title}
        revisionNumber={overview.revision.revision_number}
        back={{ text: fromWrap ? "Wrap up" : "Overview", onClick: () => goTo(fromWrap ? { kind: "wrap" } : { kind: "overview" }) }}
        view={{ current: "one", onChange: switchToAll }}
        counter={counter}
        status={counter ? undefined : { text: "Loading all parts…" }}
        onAsk={aiAvailable ? onAskChange : undefined}
        askKey
        // The phone dock has its own Ask for this part: one Ask on a phone.
        askOnPhone={!(aiAvailable && onAsk && current && !interstitial)}
        onShortcuts={() => setShortcutsOpen(true)}
        moreItems={[{ label: "All files", onSelect: switchToAll }]}
      />
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={focusLegend({ aiAsk: aiAvailable && Boolean(onAsk), testAvailable: Boolean(mapping && canWrite), bindings })} />
      <div role="region" className="min-w-0 flex-1" data-review-scroll-root="focus" aria-label="One at a time">
        <div className="flex w-full min-w-0 flex-col gap-3 px-4 py-3 pb-28 sm:px-6 md:pb-6">
          {confirmation && !interstitial ? <div role="status" className={`flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-label ${CONFIRMATION_CLASS[confirmationTone(confirmation)]}`} data-testid="focus-confirmation" data-tone={confirmationTone(confirmation)}>
            <span>{confirmationText(confirmation)}</span>
            <Button variant="link" size="compact" onClick={() => { const back = targets.find((target) => target.target_id === confirmation.part); if (back) visit(back); }}>Back to this part</Button>
          </div> : null}
          {copyStatus && <p role="status" className="text-meta text-muted-foreground">{copyStatus}</p>}
          {error ? <p role="alert" className="rounded-md border border-p1-line bg-p1-bg p-3 text-label text-p1">{error}</p> : null}
          {interstitial ? <section className="rounded-lg border border-border bg-card p-5" data-testid="focus-interstitial">
            <Check className="text-ok" size={20} aria-hidden="true" />
            <h1 ref={interstitialHeadingRef} tabIndex={-1} className="mt-3 text-title font-semibold focus:outline-none">{interstitialHeading(decisions.length)}</h1>
            <ul className="my-4 space-y-2">{decisions.map((part) => <li key={part.target_id} className="flex items-center justify-between gap-3 text-label"><span className="font-mono">{partName(part)}</span><Badge tone={part.state === "needs_changes" ? "warning" : part.state === "reviewed" ? "success" : "neutral"}>{part.state === "reviewed" || part.state === "needs_changes" ? answerBadge(part) : "Not answered"}</Badge></li>)}</ul>
            {nextQuick ? <><p className="mb-3 text-label">One small part is left. Glance and confirm.</p><Button variant="brand" onClick={() => visit(nextQuick)}>Quick check · {quick.length} {quick.length === 1 ? "part" : "parts"} <ChevronRight size={16} aria-hidden="true" /></Button></> : <Button variant="brand" onClick={() => goTo({ kind: "wrap" }, { wrapUp: true })}>Wrap up <ChevronRight size={16} aria-hidden="true" /></Button>}
          </section> : current ? (
            <section className="min-w-0" data-testid="focus-card">
              <div className="min-w-0 border-l-2 border-foreground py-0.5 pl-4 sm:pl-5">
                <div className="flex items-start justify-between gap-x-2 sm:gap-x-4">
                  <div className="min-w-0 flex-1">
                    <p className="font-mono text-tag uppercase text-muted-foreground">{isDecision ? `Part ${position.n} of ${position.total} · ${current.state === "later" ? "You left this for later" : "Needs your eyes"}` : `Part ${position.n} of ${position.total} · Quick check`}</p>
                    <div className="mt-0.5 flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
                      <h1 ref={headingRef} tabIndex={-1} className="break-all text-title font-semibold text-foreground focus:outline-none">{focusName}</h1>
                      <span className="break-all font-mono text-meta text-muted-foreground">{linesLabel(current)}</span>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    {before ? <Button variant="ghost" size="sm" aria-label="Previous part" className="max-sm:px-0" onClick={() => visit(before)}><ChevronLeft size={16} aria-hidden="true" /><span className="max-sm:hidden">Previous part</span></Button> : null}
                    {/* The part's one action is a button of its own, not a menu with a single item. */}
                    <Button variant="ghost" size="sm" aria-label="Copy link to this part" title="Copy link to this part (⌘⇧C)" className="min-w-11" data-testid="focus-copy-link" onClick={() => void copyPartLink()}><Link2 size={16} aria-hidden="true" /></Button>
                  </div>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">{fact ? <Badge tone={fact.tone} className={fact.plain ? "whitespace-normal font-sans normal-case" : undefined}>{fact.label}</Badge> : null}{fact?.policy ? <span className="text-meta text-muted-foreground" data-testid="focus-policy">{fact.policy}</span> : null}<PartProofBadge proof={proof} path={current.path} showSource={false} /><MovedFromLabel target={current} />{reportedVerdict === "PASS" ? <Badge tone="neutral">Test passed{runState?.reused ? " · reused" : ""}</Badge> : reportedVerdict === "FAIL" ? <Badge tone="warning">Test failed</Badge> : testDidNotRun(current) ? <Badge tone="warning">Tests did not run</Badge> : null}</div>
                <PartProofNote proof={proof} path={current.path} className="mt-1" />
                {mapping && <div className="mt-3 space-y-2" data-testid="focus-mapped-test">
                  <div className="flex flex-wrap items-center gap-2"><Button variant="outline" size="sm" onClick={() => void runPartTest()} disabled={!canWrite || runState?.state === "running"} aria-busy={runState?.state === "running"}>{runState?.state === "running" ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />} {runState?.state === "running" ? "Running…" : "Run test"}</Button><span className="text-meta text-muted-foreground">{mapping.title}</span></div>
                  {runState?.state && <div ref={testResultRef} tabIndex={-1} role="status" aria-live="polite" data-testid="focus-test-result" className="rounded-md border border-border p-2 text-label focus-visible:outline focus-visible:outline-2">
                    {runState?.state === "running" ? "Running…" : runState?.detail}
                  </div>}
                </div>}
                <div className={`mt-3 flex flex-wrap items-end gap-x-4 gap-y-2 ${question ? "justify-between" : "justify-end"}`}>
                  {/* "Your call" only when a question was recorded: an empty prompt is noise, not a call. */}
                  {question ? <h2 className="min-w-0 text-heading font-medium" data-testid="focus-your-call"><span className="mr-2 font-mono text-tag uppercase text-muted-foreground">Your call</span>{question}</h2> : null}
                  <CodeViewControls />
                </div>
                {hintLine && <p className="mt-2 text-meta text-muted-foreground" data-testid="focus-ai-hint">{hintLine}{" "}{hintRanges.map((range) => <Button key={`${range.start}-${range.end}`} variant="link" size="compact" onClick={() => goToLine(range.start)}>{current!.path} {range.end > range.start ? `${range.start}–${range.end}` : range.start} ›</Button>)}</p>}
                <div className="mt-2"><FocusCode key={key} reviewRef={apiReviewId} revisionId={overview.revision.id} target={current} notes={inlineNotes} aiFindings={aiFindings} onComment={canWrite ? commentOnLine : undefined} onAnnotationState={props.onAnnotationState} onSeen={(block, ms) => seen.reportSeen(block, ms, "dwell")}/></div>
                <ReviewPartPreviewDisclosure key={key + ":preview"} reviewRef={reviewRef} apiReviewId={apiReviewId} revisionId={overview.revision.id} target={current}/>
                <Button variant="link" size="sm" className="mt-3" aria-expanded={wholeOpen} onClick={() => setWholeOpen((value) => !value)}>{wholeOpen ? "Hide whole change" : "Show whole change"}</Button>
                {wholeOpen ? <div className="rounded-md border border-border p-3 text-label"><p className="mb-2 font-semibold">{props.complete ? "Whole change" : "Loaded part of change"} · {sourceCount} {sourceCount === 1 ? "file" : "files"}</p>{!props.complete && <p role="status" className="text-meta text-muted-foreground">More files may still be loading.</p>}{[...new Set(targets.map((target) => target.path))].map((path) => <div key={path} className="mb-2"><p className="break-all font-mono text-meta text-muted-foreground">{path}</p>{targets.filter((part) => part.path === path).map((part) => <Button key={part.target_id} variant="link" size="sm" onClick={() => visit(part)}>{partName(part)}</Button>)}</div>)}</div> : null}
                <Button variant="ghost" size="sm" className="mt-3 w-full justify-start border border-border text-label" aria-expanded={structureOpen} onClick={() => setStructureOpen(true)}><span>Structure · callers, calls, data path</span><span className="ml-auto text-muted-foreground">open any time</span> <ChevronRight size={14} aria-hidden="true" /></Button>
                {props.investigate ? <Button variant="ghost" size="sm" className="mt-2 w-full justify-start border border-border text-label" aria-expanded={investigateOpen} data-testid="focus-open-investigate" onClick={() => setInvestigateOpen(true)}><span>Investigate · X-Ray, assumptions, what if, blind spots</span><span className="ml-auto text-muted-foreground max-sm:hidden">this part</span> <ChevronRight size={14} aria-hidden="true" /></Button> : null}
                {isPaused ? <div role="status" className="mt-4 rounded-md border border-p2-line bg-p2-bg p-3 max-sm:hidden" data-testid="focus-pause"><p className="font-mono text-tag text-p2">{pauseHeading(sites.length)}</p><p className="my-2 text-label">{pauseReason(focusName, sites)}</p><div className="mb-2"><Badge tone="neutral">Checked in code</Badge></div><div className="flex flex-wrap gap-2"><Button ref={keepRef} variant="solid" size="sm" onClick={() => void keepAnswer()} disabled={pending}>Keep my answer</Button><Button variant="ghost" size="sm" onClick={() => void mark("unreviewed")}>Change it</Button><Button variant="link" size="sm" onClick={() => setStructureOpen(true)}>Show structure</Button></div></div> : null}
                {partNotes.length > 0 ? <div className="mt-4 space-y-2" data-testid="focus-part-notes">
                  <p className="font-mono text-tag text-muted-foreground">Notes on this part</p>
                  {partNotes.map((note) => <div key={note.id} className="rounded-md border border-border px-3 py-2 text-label">
                    <p className="break-words whitespace-pre-wrap">{note.body}</p>
                    {note.reconsidered_after_structure && note.structure_revision_id === overview.revision.id && <p className="text-meta text-muted-foreground">· changed after seeing structure</p>}
                    <p className="mt-1 text-meta text-muted-foreground">{note.anchored ? `${note.path} · ${note.start_line > 0 ? `line ${note.start_line}` : "file note"}` : "Original location unavailable · not placed on a code line"}</p>
                  </div>)}
                </div> : null}
                <div className="mt-3 flex flex-wrap gap-2 border-t border-border pt-4 max-sm:hidden">
                  <Button variant="outline" size="sm" {...answerPressed(current.state, "good")} onClick={() => void mark("reviewed")} disabled={!canWrite || pending || isPaused || Boolean(policyBlock)}><Check size={16} className="text-ok" aria-hidden="true" /> Looks good</Button>
                  <Button variant="outline" size="sm" {...answerPressed(current.state, "change")} onClick={() => setComposerOpen(true)} disabled={!canWrite || pending || isPaused || Boolean(policyBlock)}><Flag size={16} className="text-p1" aria-hidden="true" /> Needs a change</Button>
                  {isDecision ? <Button variant="outline" size="sm" {...answerPressed(current.state, "later")} title={laterReason || undefined} aria-disabled={laterReason ? true : undefined} aria-describedby={laterReason ? "focus-later-reason" : undefined} onClick={() => { if (!laterReason) void mark("later"); }} disabled={!canWrite || pending || isPaused}><Clock3 size={16} aria-hidden="true" /> Decide later</Button> : null}
                  {aiAvailable && onAsk ? <Button variant="outline" size="sm" {...ai.attrs} onClick={ai.run(ask)}><Sparkles size={16} className="text-ai" aria-hidden="true" /> Ask about this part</Button> : null}
                </div>
                {policyBlock && policyStatus?.gate ? <PolicyPartAsk reviewId={apiReviewId} gate={policyStatus.gate} /> : null}
                {laterReason ? <p id="focus-later-reason" className="mt-2 text-meta text-muted-foreground">{laterReason}</p> : null}
                <p className="mt-3 text-meta text-muted-foreground">{momentumText(next, nextQuick)}</p>
                <p className="mt-1 text-meta text-muted-foreground max-sm:hidden">{focusLegend({ aiAsk: aiAvailable && Boolean(onAsk), testAvailable: Boolean(mapping && canWrite), bindings })}</p>
              </div>
            </section>
          ) : <section><h1>No part found</h1><p>The requested part is not in this revision.</p><Button variant="ghost" onClick={() => goTo({ kind: "overview" })}>Overview</Button></section>}
          {(visibleNotes.length > 0 || notesSaved > 0 || draftKept) && <aside className="space-y-2 border-t border-border pt-2 text-meta text-muted-foreground" data-testid="focus-tray">
            <div className="flex flex-wrap items-center gap-2">
              <span>{Math.max(visibleNotes.length, notesSaved)} {Math.max(visibleNotes.length, notesSaved) === 1 ? "note" : "notes"} for {agent}</span> <span>· kept here until you send</span>
              {draftKept && <Badge tone="warning">Draft kept</Badge>}
              {visibleNotes.length > 0 && <Button variant="link" size="sm" aria-expanded={notesOpen} onClick={() => setNotesOpen((open) => !open)}>{notesOpen ? "Hide notes" : "Review notes"}</Button>}
            </div>
            {notesOpen && <ul className="space-y-2" data-testid="focus-notes-list">{visibleNotes.map((note) => <li key={note.id} className="rounded-md border border-border px-3 py-2"><span className="font-mono text-meta">{note.path}</span><p className="whitespace-pre-wrap break-words">{note.body}</p>{note.reconsidered_after_structure && note.structure_revision_id === overview.revision.id && <span className="text-muted-foreground"> · changed after seeing structure</span>}</li>)}</ul>}
          </aside>}
        </div>
      </div>
      {isPaused && current && <aside className="fixed inset-x-0 bottom-16 z-20 max-h-[40dvh] overflow-y-auto border-t border-p2-line bg-p2-bg px-2 sm:hidden" data-testid="focus-phone-pause">
        <div className="flex h-12 items-center gap-2">
          <span className="min-w-0 flex-1 truncate text-label text-p2">1 thing to check before you move on</span>
          <Button ref={mobileKeepRef} variant="solid" size="sm" onClick={() => void keepAnswer()} disabled={pending}>Keep</Button>
          <Button variant="ghost" size="sm" aria-expanded={phonePauseExpanded} aria-label={phonePauseExpanded ? "Hide details" : "Show details"} onClick={() => setPhonePauseExpanded((value) => !value)}>{phonePauseExpanded ? "Hide" : "Details"}</Button>
        </div>
        {phonePauseExpanded ? <div className="space-y-2 pb-2 text-label"><p>{pauseReason(focusName, sites)}</p><Badge tone="neutral">Checked in code</Badge><div className="flex gap-2"><Button variant="ghost" size="sm" onClick={() => setStructureOpen(true)}>Show structure</Button><Button variant="ghost" size="sm" onClick={() => void mark("unreviewed")}>Change it</Button></div></div> : null}
      </aside>}
      {!interstitial && current && <div className={`fixed inset-x-0 bottom-0 z-20 grid h-16 ${aiAvailable && onAsk ? "grid-cols-4" : "grid-cols-3"} gap-1 border-t border-border bg-card p-1 sm:hidden`} data-testid="focus-phone-dock">
        {/* Full names, wrapping to two lines in a 390 px column rather than shortening (review-6.5). */}
        <Button variant="outline" {...pressedIn(PHONE_DOCK_BUTTON, current.state, "good")} disabled={!canWrite || pending || isPaused || Boolean(policyBlock)} onClick={() => void mark("reviewed")}>Looks good</Button>
        <Button variant="outline" {...pressedIn(PHONE_DOCK_BUTTON, current.state, "change")} disabled={!canWrite || pending || isPaused || Boolean(policyBlock)} onClick={() => setComposerOpen(true)}>Needs a change</Button>
        <Button variant="outline" {...pressedIn(PHONE_DOCK_BUTTON, current.state, "later")} aria-disabled={laterReason || !isDecision ? true : undefined} aria-describedby={laterReason ? "focus-later-reason" : undefined} disabled={!canWrite || pending || isPaused} onClick={() => { if (isDecision && !laterReason) void mark("later"); }}>Decide later</Button>
        {aiAvailable && onAsk ? <Button variant="outline" className={PHONE_DOCK_BUTTON} aria-label="Ask about this part" {...ai.attrs} onClick={ai.run(ask)}>Ask</Button> : null}
      </div>}
      <Sheet open={structureOpen} onOpenChange={setStructureOpen}>
        <SheetContent side="right" aria-describedby={undefined}>
          <SheetHead title={`Structure · ${focusName}${overview.revision.head_sha ? ` · checked in the code at ${overview.revision.head_sha.slice(0, 7)}` : ""}`} />
          <SheetBody>{current && <FocusStructurePanel target={current} projection={projection} />}</SheetBody>
        </SheetContent>
      </Sheet>
      {props.investigate ? <Sheet open={investigateOpen} onOpenChange={setInvestigateOpen}>
        <SheetContent side="right" aria-describedby={undefined}>
          <SheetHead title={`Investigate · ${focusName}`} />
          <SheetBody>{current && investigateOpen ? <Suspense fallback={<p role="status" className="text-meta text-muted-foreground">Loading Investigate…</p>}>
            <Investigate
              {...props.investigate}
              target={current}
              onWalk={(location) => { setInvestigateOpen(false); goTo({ kind: "xray", at: location.xray ?? "" }); }}
              onOpenDecision={(key) => { setInvestigateOpen(false); goTo({ kind: "brief" }); revealBriefDecision(key); }}
              onOpenTarget={investigateTarget}
              onOpenCodeLocation={investigateCode}
              // This part's evidence is on the Focus card (its proof and test), under the sheet.
              onInspectEvidence={() => setInvestigateOpen(false)}
            />
          </Suspense> : null}</SheetBody>
        </SheetContent>
      </Sheet> : null}
      <Dialog open={composerOpen} onOpenChange={(open) => { if (!open) closeComposer(); else setComposerOpen(true); }}>
        <DialogContent aria-describedby={undefined} data-focus-composer="true" className="max-sm:bottom-0 max-sm:top-auto max-sm:translate-y-0">
          <DialogHeader><DialogTitle>Note for {agent}</DialogTitle></DialogHeader>
          <DialogBody><p className="mb-2 break-all font-mono text-meta text-muted-foreground">{anchor}</p>
            {!draft.trim() && aiFindings.length > 0 && <div className="mb-3 flex flex-wrap items-center gap-2 border-l-[3px] border-ai px-3 py-2 text-meta">
              <span className="text-ai">AI draft available ·</span>
              <Button variant="link" size="sm" onClick={() => setDraft(aiFindings[0].body)}>Use it</Button>
              <Button variant="link" size="sm" className="break-all text-muted-foreground" onClick={() => { closeComposer(); goToLine(aiFindings[0].start_line); }}>Based on {aiFindings[0].path} · lines {aiFindings[0].start_line}–{aiFindings[0].end_line}</Button>
            </div>}
            {aiFix && <details className="mb-3 rounded-md border border-border p-3 text-label" data-testid="focus-suggested-fix">
              <summary className="cursor-pointer font-medium">Suggested fix (AI) · Show</summary>
              <pre className="my-2 max-h-44 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono text-meta">{aiFix.patch}</pre>
              <label className="flex items-center gap-2 text-label"><Checkbox checked={includedFix?.key === key && includedFix.id === aiFix.id} onChange={(event) => setIncludedFix(event.target.checked ? { key, id: aiFix.id } : null)} /> Include a suggested fix</label>
            </details>}
            <Textarea value={draft} autoFocus placeholder="Say what should change" aria-label="Say what should change" onChange={(event) => setDraft(event.target.value)} className="min-h-32 text-copy" /><p className="mt-2 text-meta text-muted-foreground">The agent receives the file, {current && current.end_line > current.start_line ? `lines ${current.start_line}–${current.end_line}` : `line ${current?.start_line ?? 0}`}, this note{includedFix?.key === key && includedFix.id === aiFix?.id ? ", and the fix" : ""}. Nothing is sent until Wrap up.{hosted ? " On a hosted review the note is kept in the hosted review meanwhile." : ""}</p>{changedAfterStructure ? <p className="text-meta text-muted-foreground">· changed after seeing structure</p> : null}</DialogBody>
          <DialogFooter><Button variant="ghost" onClick={closeComposer}>Cancel</Button><Button onClick={() => void saveNote()} disabled={pending || !canWrite || !draft.trim()}>{pending ? "Saving…" : isDecision ? "Save note & next" : "Save note"}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <FlowAskOverlay>{props.askPanel}</FlowAskOverlay>
    </>
  );
}
