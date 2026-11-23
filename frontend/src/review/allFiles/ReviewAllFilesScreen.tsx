/**
 * P3-1: one continuous, revision-pinned code review. Both reading views use
 * the Reader's authoritative mark/note callbacks; neither owns a second answer store.
 * D7 seen coverage and the signed W disclosure belong to P3-2 and must never be
 * represented by guesses about viewport exposure.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigationType } from "react-router-dom";
import { ChevronDown, Files, PanelRight, X } from "lucide-react";
import { FOLDED_GROUP, affectedSites, allFilesGroups, callersInChangedFiles, foldedGroupLabel, materialLabel } from "./allFilesModel";
import { ConfirmAllQuickChecks, type QuickConfirmActions } from "../quickCheck/ConfirmAllQuickChecks";
import { quickConfirmSplit } from "../quickCheck/confirmAllModel";
import { usePatchEvidence } from "../quickCheck/patchEvidence";
import { guardContext } from "../quickCheck/quickCheckModel";
import { ReviewAffectedSources } from "./ReviewAffectedSources";
import { ReviewPartHistory } from "./ReviewPartHistory";
import { useSeenCoverage } from "../seen/SeenCoverageProvider";
import { blocksForTarget, meterLabel, partsFullySeen, type SeenBlock } from "../seen/seenModel";

import { Button } from "../../components/ui/button";
import { Badge } from "../../components/ui/badge";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle, isDialogOpen } from "../../components/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "../../components/ui/dropdown-menu";
import { Sheet, SheetBody, SheetContent, SheetTitle } from "../../components/ui/sheet";
import { Textarea } from "../../components/ui/textarea";
import { fetchCheckRecipes, type CheckRecipesProjection } from "../checkRecipesApi";
import { runPartTest as runSharedPartTest, usePartTestRun } from "../partTestRuns";
import { revisionHasNoTestCommand, usePartProof } from "../partProof";
import { NoTestCommandLine, PartProofBadge, PartProofNote } from "../PartProofBadge";
import { CodeViewControls } from "../codeView";
import { toastAiReason, useAiGate } from "../aiBudget";
import { classifyFocus, flowAction, keyHint, screenLegend } from "../flowKeymap";
import { FLOW_RAIL_CLASS, FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf } from "../FlowTopBar";
import { hasLeftBootPath } from "../reviewBootLocation";
import ReviewViewTabs from "../ReviewViewTabs";
import { usePreviewCount } from "../usePreviewCount";
import { FocusCode, type LineComment } from "../focus/FocusCode";
import { FocusStructurePanel } from "../focus/FocusStructurePanel";
import { aiFindingsForPart, inlineNotesForPart, notesForPart, visibleReviewNotes } from "../focus/focusAnnotations";
import { LATER_OFF_REASON, counterFor, resolvePart } from "../focus/focusModel";
import { loadFocusDraft, saveFocusDraft } from "../focus/focusDraftPersistence";
import { agentName, partName, partRoute, partSlug, partsOf, plural, questionFor } from "../overview/overviewModel";
import { setDefaultView } from "../overview/defaultView";
import { getFlowBindings, useFlowBindings } from "../settings/userSettings";
import { useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { useCopyLink, useFlowPaletteScreen } from "../palette/FlowPalette";
import { buildReviewFlowPath } from "../reviewRoutes";
import { partEntries, reviewPlace } from "../palette/partEntries";
import { MovedFromLabel } from "../MovedFromLabel";
import type { Annotation, AnnotationState, MarkState, ReviewOverview, ReviewProjection, ReviewTarget } from "../types";
import { PHONE_DOCK_BUTTON, answerPressed } from "../answerTone";

export interface ReviewAllFilesScreenProps {
  reviewRef: string;
  apiReviewId: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  annotations: readonly Annotation[];
  projection: ReviewProjection | null;
  complete: boolean;
  readOnly: boolean;
  aiAvailable: boolean;
  onMark: (target: ReviewTarget, state: MarkState) => Promise<boolean | "queued">;
  /** "Confirm all N quick checks"; absent where a person cannot record answers here (read-only, attached to hosted). */
  quickConfirm?: QuickConfirmActions;
  /** A whole-part note, or (with `line`) an inline comment on a line or range written in the code. */
  onNote: (target: ReviewTarget, body: string, line?: LineComment) => Promise<boolean | "queued">;
  onAnnotationState?: (id: string, state: AnnotationState) => void;
  onAsk?: (target: ReviewTarget) => void;
  /** "Ask about the change" (the top bar). */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
}

type RailTab = "Why" | "Structure" | "Tests" | "History";
const RAIL_TABS: readonly RailTab[] = ["Why", "Structure", "Tests", "History"];
const HINT_KEY = "lemoncrow.review.allFiles.intro";
/** Cards drawn at once, and how many of them sit before the part named; and the rail rows drawn at once. */
const CARD_WINDOW = 30;
const CARD_LEAD = 10;
const RAIL_WINDOW = 120;
/** Where a jump puts a part's top: the window's scroll-padding-top (60, reviewUi.css) plus the card's scroll-mt-4. The part in
 * view is the one nearest it, so the scroll a jump makes reads back as the part jumped to, before its code has loaded too. */
const PART_LANDING_TOP = 76;

const nameOf = (target: ReviewTarget) => partName(target);
function isDone(target: ReviewTarget) {
  return target.state === "reviewed" || target.state === "needs_changes";
}
function labelFor(target: ReviewTarget): string {
  if (target.state === "reviewed") return "Looks good";
  if (target.state === "needs_changes") return "Needs a change";
  if (target.state === "later") return "You left this for later";
  return "Your call";
}

function DeferredCode(props: {
  apiReviewId: string; revisionId: string; target: ReviewTarget; notes: Annotation[]; findings: Annotation[];
  whole: boolean; onComment?: (line: LineComment) => Promise<boolean | "queued">; onAnnotationState?: (id: string, state: AnnotationState) => void;
  onSeen: (block: SeenBlock, elapsedMs: number) => void;
}) {
  const el = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    if (near) return;
    if (!el.current || typeof IntersectionObserver === "undefined") { setNear(true); return; }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { setNear(true); observer.disconnect(); }
    }, { rootMargin: "900px 0px" });
    observer.observe(el.current);
    return () => observer.disconnect();
  }, [near]);
  return <div ref={el} className="min-w-0" data-testid="all-file-code">
    {near
      ? <FocusCode reviewRef={props.apiReviewId} revisionId={props.revisionId} target={props.target} notes={props.notes} aiFindings={props.findings} preferWhole={props.whole} onComment={props.onComment} onAnnotationState={props.onAnnotationState} onSeen={props.onSeen} />
      : <p className="py-4 text-meta text-muted-foreground">Code will load as you reach this part.</p>}
  </div>;
}

export function ReviewAllFilesScreen(props: ReviewAllFilesScreenProps) {
  const { reviewRef, apiReviewId, overview, targets, annotations, projection, onMark, onNote } = props;
  const ai = useAiGate();
  const bindings = useFlowBindings();
  const revision = overview.revision.id;
  const agentLabel = overview.revision.provenance_host?.trim() ? agentName(overview.revision.provenance_host) : "the agent";
  const { goTo } = useReviewFlowNavigation(reviewRef);
  const seen = useSeenCoverage();
  const canWrite = props.complete && !props.readOnly;
  const laterOn = overview.later_marks !== false;
  const decisions = useMemo(() => partsOf(targets, "decision"), [targets]);
  const quick = useMemo(() => partsOf(targets, "quick_check"), [targets]);
  const accepted = useMemo(() => partsOf(targets, "accepted_by_rule"), [targets]);
  // Only parts accepted by rule are folded away. A quick check the server asks is never hidden by a file name
  // (a lockfile or minified path is a guess, not a verdict), so it cannot be left unanswered out of sight.
  const groups = useMemo(() => allFilesGroups(decisions, quick, accepted), [decisions, quick, accepted]);
  const hiddenIds = useMemo(() => new Set(accepted.map((target) => target.target_id)), [accepted]);
  // The same guard Quick check and the Overview batch with, over the same inputs.
  const evidence = usePatchEvidence(overview.session.id, revision);
  const guards = useMemo(() => guardContext({
    reviewId: overview.session.id, annotationReviewId: overview.session.id, revisionId: revision,
    parts: quick, annotations, failedPaths: evidence.failed,
  }), [overview.session.id, revision, quick, annotations, evidence.failed]);
  const quickSplit = useMemo(() => quickConfirmSplit(quick, guards), [quick, guards]);
  const affected = useMemo(() => affectedSites(targets, projection), [targets, projection]);
  const callersInChanged = useMemo(() => callersInChangedFiles(targets, projection), [targets, projection]);
  const ordered = useMemo(() => [...decisions, ...quick, ...accepted], [decisions, quick, accepted]);
  const [activeId, setActiveId] = useState(() =>
    resolvePart(targets, decodeURIComponent(window.location.hash.slice(1)))?.target_id ?? ordered[0]?.target_id ?? "");
  const [railOpen, setRailOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const [narrow, setNarrow] = useState(() => typeof window.matchMedia === "function" ? window.matchMedia("(max-width: 1279px)").matches : true);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(max-width: 1279px)");
    const sync = () => setNarrow(query.matches);
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);
  const [tab, setTab] = useState<RailTab>("Why");
  const [whole, setWhole] = useState(true);
  const [folderTree, setFolderTree] = useState(false);
  const [foldedOpen, setFoldedOpen] = useState(false);
  const [hint, setHint] = useState(() => {
    try { return window.localStorage.getItem(HINT_KEY) !== "dismissed"; } catch { return true; }
  });
  const [noteTarget, setNoteTarget] = useState<ReviewTarget | null>(null);
  const [draft, setDraft] = useState("");
  const [pauseTarget, setPauseTarget] = useState<{ part: ReviewTarget; state: MarkState } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // A11Y-3 (SPEC P8-2 C): nothing is announced on arrival; only changes (seen, recorded) are.
  const [announcement, setAnnouncement] = useState("");
  const [bannerVisible, setBannerVisible] = useState(false);
  const [checks, setChecks] = useState<CheckRecipesProjection | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const pending = useRef(false);
  const active = ordered.find((part) => part.target_id === activeId) ?? ordered[0] ?? null;
  const activeRun = usePartTestRun(apiReviewId, revision, active?.unit_key ?? "");
  const activeOpen = active && !isDone(active);
  const quickLeft = quick.filter((part) => !isDone(part)).length;
  const decisionsLeft = decisions.filter((part) => !isDone(part)).length;
  const location = useLocation();
  const navigationType = useNavigationType();
  const fullySeen = useMemo(() => partsFullySeen(ordered, seen.ledger), [ordered, seen.ledger]);
  const human = useMemo(() => visibleReviewNotes(annotations, overview.session.id, revision), [annotations, overview.session.id, revision]);
  const files = useMemo(() => [...new Set(ordered.map((part) => part.path))], [ordered]);
  const activeRef = useRef(active);
  activeRef.current = active;
  const sections = useRef(new Map<string, HTMLElement>());

  // A large change draws a window of its parts, not all of them: the cards around the part the link or the keys last named
  // (the window moves when a jump leaves it, never while scrolling inside it), and the rail around the part in view.
  const orderIndex = useMemo(() => new Map(ordered.map((part, index) => [part.target_id, index] as const)), [ordered]);
  const [anchorId, setAnchorId] = useState(activeId);
  const anchorIndex = orderIndex.get(anchorId) ?? 0;
  const cardStart = Math.max(0, Math.min(anchorIndex - CARD_LEAD, ordered.length - CARD_WINDOW));
  const cardEnd = Math.min(ordered.length, cardStart + CARD_WINDOW);
  // P11-1: the revision's one proof record, read once. The cards and rail rows drawn are a window, so each badge is a map lookup.
  const proof = usePartProof(apiReviewId, revision, { inView: ordered.slice(cardStart, cardEnd).map((part) => part.path), autoRun: canWrite });
  const noTestCommand = revisionHasNoTestCommand(proof);
  // The rail lists the parts in the order it shows them (review order, or path order in Folder tree), and draws a window of
  // that sequence around the part in view; "earlier" and "later" rows move the window.
  const railSequence = useMemo(() => groups.flatMap(([label, group]) =>
    label === FOLDED_GROUP && !foldedOpen ? [] : folderTree ? [...group].sort((a, b) => a.path.localeCompare(b.path)) : group), [groups, folderTree, foldedOpen]);
  const railIndex = useMemo(() => new Map(railSequence.map((part, index) => [part.target_id, index] as const)), [railSequence]);
  // Where the person moved the window to (absolute, already clamped), or null while it follows the part in view.
  const [railPin, setRailPin] = useState<number | null>(null);
  const activeRailIndex = railIndex.get(active?.target_id ?? "") ?? 0;
  const railMax = Math.max(0, railSequence.length - RAIL_WINDOW);
  const railBase = Math.max(0, Math.min(activeRailIndex - RAIL_WINDOW / 2, railMax));
  const railStart = railPin === null ? railBase : Math.max(0, Math.min(railPin, railMax));
  const railEnd = Math.min(railSequence.length, railStart + RAIL_WINDOW);
  // A shifted rail stays where the person put it while the part in view changes by scrolling; J/K, a click or a jump (and a
  // part that falls outside the shown rows) bring the rail back to the part in view.
  const passiveActive = useRef(false);
  // "earlier" focuses the new first row; "later" the first row past the old window. `list` is the list that holds the clicked button (the aside and the phone Files sheet each draw one).
  const railMoved = useRef<{ direction: "earlier" } | { direction: "later"; from: number } | null>(null);
  const railList = useRef<Element | null>(null);
  useEffect(() => {
    const passive = passiveActive.current;
    passiveActive.current = false;
    if (passive && railPin !== null && activeRailIndex >= railStart && activeRailIndex < railEnd) return;
    setRailPin(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRailIndex, folderTree]);
  // After "N earlier" or "N later" focus goes to the first row that was not shown before; the button may be gone.
  useEffect(() => {
    const moved = railMoved.current;
    if (moved === null) return;
    railMoved.current = null;
    const id = railSequence[moved.direction === "earlier" ? railStart : moved.from]?.target_id;
    if (id) (railList.current ?? document).querySelector<HTMLElement>(`[data-rail-id="${CSS.escape(id)}"]`)?.focus({ preventScroll: false });
  }, [railStart, railEnd, railSequence]);
  // The position stored is the one the window actually used (clamped), and a click that leaves the window where it is moves nothing.
  const moveRail = (event: React.MouseEvent<HTMLElement>, direction: "earlier" | "later") => {
    const next = Math.max(0, Math.min(railStart + (direction === "earlier" ? -RAIL_WINDOW / 2 : RAIL_WINDOW / 2), railMax));
    if (next === railStart) { railMoved.current = null; return; }
    railMoved.current = direction === "earlier" ? { direction } : { direction, from: railEnd };
    railList.current = event.currentTarget.closest('[data-testid="all-files-list"]');
    setRailPin(next);
  };
  const inCards = (part: ReviewTarget) => { const index = orderIndex.get(part.target_id) ?? -1; return index >= cardStart && index < cardEnd; };
  const inRail = (part: ReviewTarget) => { const index = railIndex.get(part.target_id) ?? -1; return index >= railStart && index < railEnd; };
  const windowRef = useRef({ cardStart, cardEnd, orderIndex });
  windowRef.current = { cardStart, cardEnd, orderIndex };

  const jump = useCallback((part: ReviewTarget, updateHash = true, focus = true) => {
    if (hiddenIds.has(part.target_id)) setFoldedOpen(true);
    passiveActive.current = false;
    setActiveId(part.target_id);
    const index = windowRef.current.orderIndex.get(part.target_id) ?? 0;
    const outside = index < windowRef.current.cardStart || index >= windowRef.current.cardEnd;
    if (outside) setAnchorId(part.target_id);
    const section = sections.current.get(part.target_id);
    if (section) { section.scrollIntoView({ behavior: "auto", block: "start" }); if (focus) section.querySelector<HTMLElement>("h2")?.focus({ preventScroll: true }); }
    else if (outside || hiddenIds.has(part.target_id)) {
      // The card is drawn on the next frames (the window moved, or the folded group opened).
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        const drawn = sections.current.get(part.target_id);
        drawn?.scrollIntoView({ block: "start" });
        if (focus) drawn?.querySelector<HTMLElement>("h2")?.focus({ preventScroll: true });
      }));
    }
    if (updateHash) {
      // Preserve history for view changes, not for every scroll position.
      window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}#${encodeURIComponent(partSlug(part, targets))}`);
    }
    setFilesOpen(false);
  }, [targets, hiddenIds]);

  // Re-runs when the link changes too, so "Open it" (a navigation to /all#<part> from this very screen) scrolls.
  // The router's `location.key` covers a second "Open it" for the part the first one named.
  useEffect(() => {
    const selected = resolvePart(ordered, decodeURIComponent(window.location.hash.slice(1))) ?? ordered[0];
    if (!selected) return;
    // SPEC 5.3: not on the first load. The Reader rewrites its own query and fragment (a replace) after loading, which
    // leaves the cold-load state and re-runs this effect; only a link followed (a push) or a move from another screen focuses.
    const focus = navigationType === "PUSH" || hasLeftBootPath();
    const frame = window.requestAnimationFrame(() => jump(selected, false, focus));
    return () => window.cancelAnimationFrame(frame);
  }, [revision, ordered.length, location.hash, location.key]);

  // SPEC 4.6 live region: a part's changed lines turning fully seen is announced once. A part that arrives
  // already confirmed by the server (a reload) was seen earlier and is not announced again.
  const announced = useRef<Set<string> | null>(null);
  useEffect(() => {
    const now = new Set(ordered.filter((part) => { const c = seen.coverage(part); return c.total > 0 && c.seen === c.total; }).map((part) => part.target_id));
    const before = announced.current;
    announced.current = now;
    if (!before) return;
    const fresh = ordered.filter((part) => now.has(part.target_id) && !before.has(part.target_id)
      && blocksForTarget(part).some((block) => seen.ledger.rows[block.key]?.serverRecorded !== true));
    if (fresh.length) setAnnouncement(fresh.map((part) => `${nameOf(part)}: all changed lines seen.`).join(" "));
  }, [ordered, seen.ledger]);

  // The page scrolls the window (the top bar and the tray are sticky), so both trackers listen there.
  useEffect(() => {
    let frame = 0;
    const check = () => {
      frame = 0;
      const inView = [...sections.current.entries()].map(([id, element]) => ({ id, rect: element.getBoundingClientRect() }))
        .filter(({ rect }) => rect.bottom > 130 && rect.top < window.innerHeight - 70);
      if (!inView.length) return;
      inView.sort((a, b) => Math.abs(a.rect.top - PART_LANDING_TOP) - Math.abs(b.rect.top - PART_LANDING_TOP));
      if (activeRef.current?.target_id !== inView[0].id) passiveActive.current = true;
      setActiveId((before) => before === inView[0].id ? before : inView[0].id);
    };
    const onScroll = () => { if (!frame) frame = window.requestAnimationFrame(check); };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => { window.removeEventListener("scroll", onScroll); if (frame) window.cancelAnimationFrame(frame); };
  }, [ordered.length, revision]);

  // The phone dock is only offered while the active answer banner is visible;
  // scrolling into the code or file list removes it instead of covering code.
  useEffect(() => {
    if (!activeOpen) { setBannerVisible(false); return; }
    let frame = 0;
    const measure = () => {
      frame = 0;
      const banner = sections.current.get(active!.target_id)?.querySelector<HTMLElement>('[data-testid="all-files-answer"]');
      // The reading area: below the sticky top bar, above whichever bottom bar is showing. On a phone the dock
      // replaces the tray (review-9.7), and a hidden tray measures zero, so it can't be the edge.
      const top = document.querySelector('[data-testid="flow-top-bar"]')?.getBoundingClientRect().bottom ?? 0;
      const bars = [...document.querySelectorAll<HTMLElement>('[data-testid="all-files-tray"], [data-testid="all-files-phone-dock"]')]
        .map((bar) => bar.getBoundingClientRect()).filter((rect) => rect.height > 0);
      const bottom = bars.length ? Math.min(...bars.map((rect) => rect.top)) : window.innerHeight;
      const visible = banner?.getBoundingClientRect();
      setBannerVisible(Boolean(visible && visible.bottom > top && visible.top < bottom));
    };
    const handle = () => { if (!frame) frame = window.requestAnimationFrame(measure); };
    measure();
    window.addEventListener("scroll", handle, { passive: true });
    window.addEventListener("resize", handle);
    return () => { window.removeEventListener("scroll", handle); window.removeEventListener("resize", handle); if (frame) window.cancelAnimationFrame(frame); };
  }, [active?.target_id, activeOpen, ordered.length]);

  useEffect(() => {
    let live = true;
    setChecks(null);
    fetchCheckRecipes(apiReviewId, revision).then((result) => {
      if (live && result.revision_id === revision) setChecks(result);
    }).catch(() => { /* No trusted mapping means no Run test button. */ });
    return () => { live = false; };
  }, [apiReviewId, revision]);

  const openNote = useCallback((part: ReviewTarget) => {
    setPauseTarget(null);
    setNoteTarget(part);
    setDraft(loadFocusDraft(apiReviewId, revision, part.unit_key));
    setError("");
  }, [apiReviewId, revision]);
  const saveNote = useCallback(async () => {
    if (!noteTarget || !draft.trim() || !canWrite || pending.current) return;
    pending.current = true; setBusy(true); setError("");
    try {
      const result = await onNote(noteTarget, draft.trim());
      if (result !== true) { setError(result === "queued" ? "The note is queued and awaiting server confirmation." : "The note could not be saved; your draft is kept."); return; }
      saveFocusDraft(apiReviewId, revision, noteTarget.unit_key, "");
      setAnnouncement(`${nameOf(noteTarget)}: Needs a change recorded.`);
      setNoteTarget(null); setDraft("");
    } catch { setError("The note could not be saved; your draft is kept."); }
    finally { pending.current = false; setBusy(false); }
  }, [apiReviewId, canWrite, draft, noteTarget, onNote, revision]);

  const mark = useCallback(async (part: ReviewTarget, state: MarkState, confirmed = false) => {
    if (!canWrite || pending.current) return;
    if (state === "later" && !laterOn) return;
    if (!confirmed && isDone(part) && part.state !== state) { setPauseTarget({ part, state }); return; }
    pending.current = true; setBusy(true); setError("");
    try {
      const result = await onMark(part, state);
      if (result !== true) { setError(result === "queued" ? "The answer is queued and awaiting server confirmation." : "The answer could not be saved."); return; }
      setAnnouncement(`${nameOf(part)}: ${state === "reviewed" ? "Looks good" : state === "later" ? "Decide later" : "Answer"} recorded.`);
    } catch { setError("The answer could not be saved."); }
    finally { pending.current = false; setBusy(false); }
  }, [canWrite, laterOn, onMark]);

  const beginNeedsChange = useCallback((part: ReviewTarget) => {
    if (isDone(part) && part.state !== "needs_changes") setPauseTarget({ part, state: "needs_changes" });
    else openNote(part);
  }, [openNote]);

  const runTest = useCallback(async (part: ReviewTarget) => {
    const mapping = checks?.revision_id === revision ? checks.part_checks?.find((row) => row.unit_key === part.unit_key && row.path === part.path && row.symbol === part.symbol) : undefined;
    if (!mapping || !canWrite) return;
    // One state per part, shared with the Focus card and Quick check; the run sends `fresh: true`.
    await runSharedPartTest({ reviewId: apiReviewId, revisionId: revision, unitKey: part.unit_key, mapping, path: part.path });
  }, [apiReviewId, canWrite, checks, revision]);

  const switchToFocus = useCallback(() => {
    if (!activeRef.current) return;
    setDefaultView("one");
    goTo(partRoute(activeRef.current, ordered));
  }, [goTo, ordered]);

  const actionRef = useRef({ ordered, files, active, canWrite, busy, mark, beginNeedsChange, jump, goTo, switchToFocus, runTest, onAsk: props.onAsk, aiBlocked: ai.reason });
  actionRef.current = { ordered, files, active, canWrite, busy, mark, beginNeedsChange, jump, goTo, switchToFocus, runTest, onAsk: props.onAsk, aiBlocked: ai.reason };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const state = actionRef.current;
      const el = document.activeElement as HTMLElement | null;
      const action = flowAction(event, {
        screen: "allFiles",
        bindings: getFlowBindings(),
        focus: el ? classifyFocus({ tagName: el.tagName, role: el.getAttribute("role"), isContentEditable: el.isContentEditable }) : "other",
        surface: isDialogOpen() ? "other" : "none",
        isDecision: state.active ? decisions.some((part) => part.target_id === state.active!.target_id) : false,
        aiAsk: Boolean(state.onAsk),
        aiBlocked: state.aiBlocked,
        testAvailable: true,
      });
      if (!action || state.busy) return;
      const selected = state.active;
      const i = selected ? state.ordered.findIndex((part) => part.target_id === selected.target_id) : -1;
      const fi = selected ? state.files.indexOf(selected.path) : -1;
      switch (action.intent) {
        case "looksGood": if (selected) void state.mark(selected, "reviewed"); break;
        case "needsChange": if (selected) state.beginNeedsChange(selected); break;
        case "decideLater": if (selected) void state.mark(selected, "later"); break;
        case "nextPart": if (state.ordered[i + 1]) state.jump(state.ordered[i + 1]); break;
        case "previousPart": if (i > 0) state.jump(state.ordered[i - 1]); break;
        case "nextFile": {
          const next = state.ordered.find((part) => part.path === state.files[fi + 1]); if (next) state.jump(next); break;
        }
        case "previousFile": {
          const previous = state.ordered.find((part) => part.path === state.files[fi - 1]); if (previous) state.jump(previous); break;
        }
        case "help": setShortcutsOpen(true); break;
        case "switchView": state.switchToFocus(); break;
        case "nextUnseenRisky": seen.nextUnseen(); break;
        case "wrapUp": state.goTo({ kind: "wrap" }, { wrapUp: true }); break;
        case "ask": if (selected) state.onAsk?.(selected); break;
        case "aiUnavailable": toastAiReason(action.reason ?? state.aiBlocked); break;
        case "runTest": if (selected) void state.runTest(selected); break;
        default: return;
      }
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [decisions, seen.nextUnseen]);

  const dismissHint = () => {
    setHint(false);
    try { window.localStorage.setItem(HINT_KEY, "dismissed"); } catch { /* Private mode is acceptable. */ }
  };
  const fileList = <div className="space-y-3" data-testid="all-files-list">
    <div className="flex items-center justify-between gap-2"><p className="text-label font-semibold">Files · ordered for review</p>
      <Button variant="ghost" size="xs" onClick={() => setFolderTree((before) => !before)}>{folderTree ? "Review order" : "Folder tree"}</Button>
    </div>
    <p className="text-meta text-muted-foreground">Decisions first, then quick checks.</p>
    {railStart > 0 && <Button variant="outline" size="sm" className="w-full" data-testid="all-files-rail-earlier" onClick={(event) => moveRail(event, "earlier")}>{railStart} earlier</Button>}
    {groups.map(([label, group]) => group.length > 0 && <section key={label} aria-label={label}>
      <p className="mb-2 font-mono text-tag text-muted-foreground">{label === FOLDED_GROUP ? foldedGroupLabel(group) : `${label} · ${group.length}`}</p>
      {label === FOLDED_GROUP && <Button variant="outline" size="sm" className="mb-2 w-full" aria-expanded={foldedOpen} data-testid="all-files-folded-toggle" onClick={() => setFoldedOpen((before) => !before)}>{foldedOpen ? "Fold accepted files" : "Show accepted files"}</Button>}
      {(label !== FOLDED_GROUP || foldedOpen) && (folderTree ? [...group].sort((a, b) => a.path.localeCompare(b.path)) : group).filter(inRail).map((part) =>
        <Button key={part.target_id} variant="ghost" unstyled type="button" data-testid="all-files-rail-row" data-rail-id={part.target_id} aria-current={activeId === part.target_id ? "location" : undefined}
          className={`mb-1 flex w-full min-w-0 items-center gap-2 rounded-md border px-2 py-2 text-left text-meta ${activeId === part.target_id ? "border-foreground bg-accent" : "border-transparent hover:bg-accent"}`}
          onClick={() => jump(part)}>
          <span className={`h-2 w-2 shrink-0 rounded-full ${part.state === "needs_changes" ? "bg-p0-solid" : part.state === "reviewed" ? "bg-ok-solid" : part.state === "later" ? "bg-p2-solid" : "bg-border"}`} aria-hidden="true" />
          <span className="min-w-0 flex-1"><span className="block truncate font-medium">{nameOf(part)}</span><span className={`block truncate ${activeId === part.target_id ? "text-foreground" : "text-muted-foreground"}`} title={part.path}>{part.path}{materialLabel(part.path, projection) ? ` · ${materialLabel(part.path, projection)}` : ""}</span>
            {seen.coverage(part).linesTotal > 0 && <span className={`block truncate ${activeId === part.target_id ? "text-foreground" : "text-muted-foreground"}`} data-testid="seen-file-meter">{meterLabel(seen.coverage(part).linesSeen, seen.coverage(part).linesTotal)}</span>}</span>
        </Button>)}
    </section>)}
    {railEnd < railSequence.length && <Button variant="outline" size="sm" className="w-full" data-testid="all-files-rail-later" onClick={(event) => moveRail(event, "later")}>{railSequence.length - railEnd} later</Button>}
    {ordered.length === 0 && <p role="status" className="text-meta text-muted-foreground">There are no parts to display in this revision.</p>}
    {fullySeen.total > 0 && <p className="border-t border-border pt-2 text-meta text-muted-foreground" data-testid="seen-coverage-footer">Coverage · {fullySeen.seen} of {fullySeen.total} parts fully seen</p>}
  </div>;

  const rail = active && <div className="space-y-4" data-testid="all-files-context">
    <h2 className="text-label font-semibold">Context for {nameOf(active)}</h2>
    <div role="tablist" aria-label="Part context" className="flex flex-wrap gap-1">
      {RAIL_TABS.map((name) => <Button key={name} variant="ghost" unstyled role="tab" aria-selected={tab === name} type="button"
        onClick={() => setTab(name)} onKeyDown={(event) => {
          if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
          event.preventDefault();
          const index = RAIL_TABS.indexOf(tab);
          const next = RAIL_TABS[(index + (event.key === "ArrowRight" ? 1 : -1) + RAIL_TABS.length) % RAIL_TABS.length];
          setTab(next);
          (event.currentTarget.parentElement?.querySelectorAll<HTMLElement>('[role="tab"]')[RAIL_TABS.indexOf(next)])?.focus();
        }}
        className={`rounded-md px-2 py-1.5 text-meta ${tab === name ? "bg-accent font-semibold" : "text-muted-foreground hover:bg-accent"}`}>{name}</Button>)}
    </div>
    <div role="tabpanel" className="space-y-3">
      {tab === "Why" && <><h3 className="text-label font-semibold">Your question</h3><p className="text-label">{questionFor(active, projection)}</p>
        {active.reasons.length > 0 && <p className="text-meta text-muted-foreground">Review signals: {active.reasons.join(", ").replaceAll("_", " ")}</p>}
        <p className="text-meta text-muted-foreground">The author's explanation is not evidence of the change.</p>
      </>}
      {tab === "Structure" && <FocusStructurePanel target={active} projection={projection} />}
      {tab === "Tests" && (() => {
        const mapped = checks?.revision_id === revision ? checks.part_checks?.find((row) => row.unit_key === active.unit_key && row.path === active.path && row.symbol === active.symbol) : undefined;
        return <><h3 className="text-label font-semibold">Tests for this part</h3>
          <PartProofNote proof={proof} path={active.path} />
          {mapped ? <Button variant="outline" size="sm" disabled={!canWrite || busy} onClick={() => void runTest(active)}>Run test</Button>
            : <p className="text-meta text-muted-foreground">No exact trusted-base test mapping was returned for this part.</p>}
          {activeRun && <p role="status" className="text-meta" data-testid="all-test-result">{activeRun.detail}</p>}
        </>;
      })()}
      {tab === "History" && <ReviewPartHistory apiReviewId={apiReviewId} revisionId={revision} revisionNumber={overview.revision.revision_number} part={active} notes={notesForPart(human, active)}/>}
    </div>
  </div>;

  const counter = counterFor(targets, active?.target_id ?? "", props.complete);
  const phoneDock = Boolean(active && activeOpen && bannerVisible);
  const previewCount = usePreviewCount(apiReviewId, revision, targets);
  const activeHasTest = Boolean(active && checks?.revision_id === revision && checks.part_checks?.some((row) => row.unit_key === active.unit_key && row.path === active.path && row.symbol === active.symbol));

  const copyLink = useCopyLink();
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "all"),
    place: reviewPlace(repoNameOf(overview), overview.title, "All files"),
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "allFiles",
      part: active ? partName(active) : undefined,
      canAnswer: canWrite && !busy && Boolean(active),
      isDecision: laterOn && Boolean(active && decisions.some((part) => part.target_id === active.target_id)),
      aiAsk: Boolean(props.onAsk) && Boolean(active),
      view: "all",
      actions: {
        looksGood: () => { if (active) void mark(active, "reviewed"); },
        needsChange: () => { if (active) beginNeedsChange(active); },
        decideLater: () => { if (active) void mark(active, "later"); },
        switchView: switchToFocus,
        wrapUp: () => goTo({ kind: "wrap" }, { wrapUp: true }),
        copyLink: () => { if (active) void copyLink?.(new URL(buildReviewFlowPath(reviewRef, { kind: "all", part: partSlug(active, targets) }), window.location.href).href); },
        ask: ai.run(() => { if (active) props.onAsk?.(active); }),
      },
    },
  });

  return <>
    <FlowTopBar
      repo={repoNameOf(overview)}
      documentTitle={active ? `All files: ${nameOf(active)}` : "All files"}
      title={overview.title}
      revisionNumber={overview.revision.revision_number}
      back={{ text: "Overview", onClick: () => goTo({ kind: "overview" }) }}
      view={{ current: "all", onChange: switchToFocus }}
      counter={counter}
      status={counter ? undefined : { text: "Loading all parts…" }}
      onAsk={props.aiAvailable ? props.onAskChange : undefined}
      askKey
      onShortcuts={() => setShortcutsOpen(true)}
      moreItems={[
        { label: "One at a time", onSelect: switchToFocus },
        // A phone shows one bar at the bottom: while the answer dock is up the tray's two actions live here too (review-9.7).
        { label: "Next unseen risky part", onSelect: () => seen.nextUnseen() },
        ...(props.complete ? [{ label: "Wrap up", onSelect: () => goTo({ kind: "wrap" }, { wrapUp: true }) }] : []),
      ]}
    />
    <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={screenLegend("allFiles", { aiAsk: Boolean(props.onAsk), testAvailable: activeHasTest, bindings })} />
    <div className="flex shrink-0 items-center border-b border-border" data-testid="all-files-header">
      <div className="min-w-0 flex-1"><ReviewViewTabs reviewRef={reviewRef} current="diff" previewCount={previewCount} /></div>
      <div className="flex shrink-0 items-center gap-1 px-3">
        <Button variant="outline" size="sm" className="xl:hidden" onClick={() => setFilesOpen(true)}><Files size={16} aria-hidden="true"/> Files</Button>
        <Button variant="ghost" size="sm" onClick={() => setRailOpen((before) => !before)}><PanelRight size={16} aria-hidden="true"/> Context</Button>
      </div>
    </div>
    <div className="flex min-w-0 flex-1">
      {/* pb-16: the list's last rows scroll clear of the sticky tray. */}
      <aside className={`${FLOW_RAIL_CLASS} hidden w-[280px] shrink-0 border-r border-border p-3 pb-16 xl:block`} aria-label="Review files">{fileList}</aside>
      <div role="region" className="min-w-0 flex-1" aria-label="All files" data-review-scroll-root="all-files" data-testid="all-files-view">
        <div className="min-w-0 space-y-6 px-4 py-4 pb-28 sm:px-6">
          {hint && <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-accent p-3 text-label" data-testid="all-files-hint">
            <span>Same review, all files at once. Your answers carry over.</span><Button variant="ghost" size="sm" onClick={dismissHint}><X size={14} aria-hidden="true"/> Dismiss</Button>
          </div>}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h1 ref={heading} tabIndex={-1} className="text-title font-semibold focus:outline-none">All files</h1>
              <p className="text-meta text-muted-foreground">{files.length} files · {ordered.length} parts · {plural(decisionsLeft, "decision")} left · {quickLeft} of {quick.length} quick checks left · {ordered.reduce((n, part) => n + seen.coverage(part).linesSeen, 0)} of {ordered.reduce((n, part) => n + seen.coverage(part).linesTotal, 0)} changed lines seen · seeing lines doesn't count as reviewing them</p>
              <NoTestCommandLine proof={proof} />
            </div>
            <div className="flex flex-wrap items-center gap-1"><CodeViewControls />
            <DropdownMenu><DropdownMenuTrigger asChild><Button variant="outline" size="sm">View <ChevronDown size={14} aria-hidden="true"/></Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onSelect={() => setWhole(true)}>Whole function{whole ? " ✓" : ""}</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setWhole(false)}>Changes only{whole ? "" : " ✓"}</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu></div>
          </div>
          {!props.complete && <p role="alert" className="rounded-md border border-p1-line p-3 text-label">The list of parts is still loading. Answer controls are disabled until it is complete.</p>}
          {error && <p role="alert" className="rounded-md border border-p1-line p-3 text-label">{error}</p>}
          {cardStart > 0 && <Button variant="outline" size="sm" data-testid="all-files-earlier" onClick={() => jump(ordered[cardStart - 1])}>Show earlier parts · {cardStart} before these {cardEnd - cardStart}</Button>}
          {groups.map(([label, group]) => group.length > 0 && <section key={label} className="space-y-3" aria-label={label + " parts"}>
            <h2 className="border-b border-border pb-2 font-mono text-tag text-muted-foreground">{label === FOLDED_GROUP ? foldedGroupLabel(group) : `${label} · ${group.length}`}</h2>
            {label === "Quick check" && props.quickConfirm && canWrite && <ConfirmAllQuickChecks parts={quickSplit.confirm} ownAnswer={quickSplit.ownAnswer} apiReviewId={apiReviewId} sessionId={overview.session.id} revisionId={revision} actions={props.quickConfirm} />}
            {label === FOLDED_GROUP && <p className="text-meta text-muted-foreground">A repository rule accepted these files (generated files, lockfiles). They are not counted as work.</p>}
            {label === FOLDED_GROUP && <Button size="sm" variant="outline" aria-expanded={foldedOpen} onClick={() => setFoldedOpen((before) => !before)}>{foldedOpen ? "Fold accepted files" : `Show ${group.length} accepted ${group.length === 1 ? "part" : "parts"}`}</Button>}
            {(label !== FOLDED_GROUP || foldedOpen) && group.filter(inCards).map((part) => {
              const notes = notesForPart(human, part);
              const findings = aiFindingsForPart(annotations, overview.session.id, revision, part, props.aiAvailable);
              const isActive = activeId === part.target_id;
              return <article key={part.target_id} ref={(node) => { if (node) sections.current.set(part.target_id, node); else sections.current.delete(part.target_id); }}
                className={`min-w-0 scroll-mt-4 space-y-3 border-l-2 pl-3 sm:pl-4 ${isActive ? "border-l-brand" : "border-l-transparent"}`}
                data-testid="all-files-part" data-part-id={part.target_id}>
                <div className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-2">
                  <div className="min-w-0">
                    <h2 tabIndex={-1} className="break-words text-heading font-semibold focus:outline-none">{nameOf(part)}</h2>
                    <p className="break-all font-mono text-tag text-muted-foreground">{part.path} · lines {part.start_line}–{part.end_line}</p>
                    <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-meta text-muted-foreground"><span data-testid="seen-part-count">{seen.coverage(part).linesTotal ? `${seen.coverage(part).linesSeen} of ${seen.coverage(part).linesTotal} changed lines seen` : "Changed-line viewing not measurable"}</span>
                      <PartProofBadge proof={proof} path={part.path} showReason saidOnScreen={noTestCommand} />
                      <MovedFromLabel target={part} />
                      {part.attention_level === "high" ? <Badge tone="warning">Needs attention</Badge> : null}
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-1">
                    <Button variant="ghost" size="xs" onClick={() => { setActiveId(part.target_id); setTab("Why"); setRailOpen(true); }}>Context</Button>
                    <Button variant="ghost" size="xs" onClick={() => goTo(partRoute(part, targets))}>One at a time</Button>
                  </div>
                </div>
                {/* "Your call": one compact row of answers under the part's name, not a separate boxed card. */}
                {label !== "Accepted by rule" && <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2" data-testid="all-files-answer">
                  <span className="font-mono text-tag uppercase text-muted-foreground">{labelFor(part)}</span>
                  {!isDone(part) && questionFor(part, projection) ? <span className="min-w-0 text-label">{questionFor(part, projection)}</span> : null}
                  {part.state === "reviewed" && <span className="text-meta text-muted-foreground">Looks good · you · recorded</span>}
                  {part.state === "needs_changes" && <span className="text-meta text-muted-foreground">Needs a change · your note stays in this review until Wrap up.</span>}
                  {part.state === "later" && <span className="text-meta text-muted-foreground">You left this for later. This is not a reviewed answer.</span>}
                  {pauseTarget?.part.target_id === part.target_id ? <div role="group" aria-label="Review your earlier answer" className="flex flex-wrap items-center gap-2">
                    <span className="text-label">You already answered this part. Keep your answer or change it?</span>
                    <Button size="sm" variant="outline" onClick={() => setPauseTarget(null)}>Keep my answer</Button>
                    <Button size="sm" onClick={() => { const intended = pauseTarget.state; setPauseTarget(null); if (intended === "reviewed") void mark(part, "reviewed", true); else openNote(part); }}>Change it</Button>
                  </div> : <>
                  {/* A phone answers with the dock at the bottom (SPEC 4.6): the card does not repeat its four buttons. */}
                  <span className="text-meta text-muted-foreground sm:hidden">Answer with the buttons at the bottom of the screen.</span>
                  <div className="flex flex-wrap items-center gap-1 max-sm:hidden">
                    <Button variant="outline" size="sm" {...answerPressed(part.state, "good")} disabled={!canWrite || busy} onClick={() => void mark(part, "reviewed")}>Looks good <kbd className="ml-1 hidden text-tag sm:inline">{keyHint("looksGood", bindings)}</kbd></Button>
                    <Button variant="outline" size="sm" {...answerPressed(part.state, "change")} disabled={!canWrite || busy} onClick={() => beginNeedsChange(part)}>Needs a change <kbd className="ml-1 hidden text-tag sm:inline">{keyHint("needsChange", bindings)}</kbd></Button>
                    {label === "Decisions" && <><Button variant="ghost" size="sm" {...answerPressed(part.state, "later")} disabled={!canWrite || busy} aria-disabled={laterOn ? undefined : true} aria-describedby={laterOn ? undefined : `all-later-reason-${part.target_id}`} onClick={() => { if (laterOn) void mark(part, "later"); }}>Decide later <kbd className="ml-1 hidden text-tag sm:inline">{keyHint("decideLater", bindings)}</kbd></Button>
                      {!laterOn && <p id={`all-later-reason-${part.target_id}`} className="basis-full text-meta text-muted-foreground" data-testid="all-later-reason">{LATER_OFF_REASON}</p>}</>}
                    {props.aiAvailable && props.onAsk && <Button variant="ghost" size="sm" disabled={!canWrite} {...ai.attrs} onClick={ai.run(() => props.onAsk?.(part))}>Ask about this part</Button>}
                  </div></>}
                </div>}
                <DeferredCode apiReviewId={apiReviewId} revisionId={revision} target={part} notes={inlineNotesForPart(annotations, overview.session.id, revision, part)} findings={findings} whole={whole} onComment={canWrite ? (line) => onNote(part, line.body, line) : undefined} onAnnotationState={props.onAnnotationState} onSeen={(block, ms) => seen.reportSeen(block, ms, "dwell")}/>
                {findings.map((finding) => <div key={finding.id} className="border-l-[3px] border-l-ai pl-3 text-meta" data-testid="all-files-ai-hint">
                  <p className="font-medium text-ai">AI hint · lines {finding.start_line}–{finding.end_line} · based on {finding.path}</p><p>{finding.body}</p><p className="text-muted-foreground">A hint to read the lines, not a decision.</p>
                </div>)}
                {notes.map((note) => <div key={note.id} className="border-l-[3px] border-l-p2-solid pl-3 text-meta" data-testid="all-files-human-note">
                  <p className="font-medium">Your note · {note.file_level ? "file" : `line ${note.start_line}`}</p><p>{note.body}</p><p className="text-muted-foreground">For the author. Nothing is sent until Wrap up.</p>
                </div>)}
              </article>;
            })}
          </section>)}
          {cardEnd < ordered.length && <Button variant="outline" size="sm" data-testid="all-files-later" onClick={() => jump(ordered[cardEnd])}>Show later parts · {ordered.length - cardEnd} after these {cardEnd - cardStart}</Button>}
          <ReviewAffectedSources reviewId={apiReviewId} sites={affected} inChangedFiles={callersInChanged} onJumpToChanged={(id) => { const part = ordered.find((target) => target.target_id === id); if (part) jump(part); }}/>
          {ordered.length === 0 && <p role="status">No parts were returned for this revision.</p>}
          <p className="text-meta text-muted-foreground">Seen is a reviewer-reported on-screen observation, never a human approval. Unconfirmed exposure is not included in a signed record.</p>
        </div>
      </div>
      {railOpen && <aside className={`${FLOW_RAIL_CLASS} hidden w-[360px] shrink-0 border-l border-border p-4 pb-16 xl:block`} aria-label="Part context">
        <div className="mb-3 flex justify-end"><Button variant="ghost" size="xs" onClick={() => setRailOpen(false)}>Close context</Button></div>{rail}</aside>}
    </div>
    {/* One bar at the bottom of a phone (review-9.7): the answer dock takes the tray's place while a banner is in view. */}
    {phoneDock && active && <div className="fixed inset-x-0 bottom-0 z-20 grid h-16 grid-cols-4 gap-1 border-t border-border bg-card p-1 sm:hidden" data-testid="all-files-phone-dock">
      <Button variant="outline" className={PHONE_DOCK_BUTTON} disabled={!canWrite || busy} onClick={() => void mark(active, "reviewed")}>Looks good</Button>
      <Button variant="outline" className={PHONE_DOCK_BUTTON} disabled={!canWrite || busy} onClick={() => beginNeedsChange(active)}>Needs a change</Button>
      <Button variant="outline" className={PHONE_DOCK_BUTTON} disabled={!canWrite || busy || !decisions.some((part) => part.target_id === active.target_id)} aria-disabled={laterOn ? undefined : true} aria-describedby={laterOn ? undefined : "all-later-reason-dock"} onClick={() => { if (laterOn) void mark(active, "later"); }}>Decide later</Button>{!laterOn && <span id="all-later-reason-dock" className="sr-only">{LATER_OFF_REASON}</span>}
      <Button variant="outline" className={PHONE_DOCK_BUTTON} disabled={!props.aiAvailable || !props.onAsk} aria-label="Ask about this part" {...ai.attrs} onClick={ai.run(() => props.onAsk?.(active))}>Ask</Button>
    </div>}
    <footer className={`sticky bottom-0 z-20 flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-border bg-background px-4 py-2 xl:pl-[calc(280px+1rem)] ${phoneDock ? "max-sm:hidden" : ""}`} data-testid="all-files-tray">
      <p className="hidden min-w-0 truncate text-meta text-muted-foreground sm:block">Keys act on: {active ? nameOf(active) : "no part"} · J / K moves · A looks good · C needs a change</p>
      <Button variant="outline" size="sm" onClick={() => seen.nextUnseen()}>Next unseen risky part <kbd className="ml-1 hidden text-tag sm:inline">U</kbd></Button>
      <Button variant={decisionsLeft > 0 ? "outline" : "solid"} size="sm" disabled={!props.complete} onClick={() => goTo({ kind: "wrap" }, { wrapUp: true })}>Wrap up{decisionsLeft > 0 ? ` · ${decisionsLeft} ${decisionsLeft === 1 ? "decision" : "decisions"} left` : ""}</Button>
    </footer>
    <Sheet open={filesOpen} onOpenChange={setFilesOpen}><SheetContent side="left" className="w-[min(90vw,360px)]"><SheetTitle>Files</SheetTitle><SheetBody>{fileList}</SheetBody></SheetContent></Sheet>
    <Sheet open={railOpen && narrow} onOpenChange={setRailOpen}><SheetContent side="right" className="w-[min(95vw,400px)] xl:hidden"><SheetTitle>Part context</SheetTitle><SheetBody>{rail}</SheetBody></SheetContent></Sheet>
    <Dialog open={noteTarget !== null} onOpenChange={(open) => { if (!open) setNoteTarget(null); }}>
      <DialogContent aria-describedby={undefined}>
        {/* One composer title on every screen (SPEC 4.4): "Note for <agent>", with the part under it. */}
        <DialogHeader><DialogTitle>Note for {agentLabel}</DialogTitle></DialogHeader>
        <DialogBody className="space-y-3"><p className="break-all font-mono text-meta text-muted-foreground">{noteTarget ? `${noteTarget.path} · ${nameOf(noteTarget)}` : ""}</p><p className="text-meta text-muted-foreground">Your note stays private to this review until you send it in Wrap up.</p>
          <Textarea aria-label="Say what should change" value={draft} onChange={(event) => { setDraft(event.target.value); if (noteTarget) saveFocusDraft(apiReviewId, revision, noteTarget.unit_key, event.target.value); }} rows={4} />
          {error && <p role="alert" className="text-label text-p1">{error}</p>}
        </DialogBody>
        <DialogFooter><Button variant="outline" onClick={() => setNoteTarget(null)}>Cancel</Button><Button disabled={!canWrite || busy || !draft.trim()} onClick={() => void saveNote()}>Save note</Button></DialogFooter>
      </DialogContent>
    </Dialog>
    <p aria-live="polite" className="sr-only">{announcement}</p>
    <FlowAskOverlay>{props.askPanel}</FlowAskOverlay>
  </>;
}
