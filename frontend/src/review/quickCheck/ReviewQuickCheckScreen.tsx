import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronRight, Flag, LoaderCircle, Play, Sparkles } from "lucide-react";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogTitle, isDialogOpen } from "../../components/ui/dialog";
import { Textarea } from "../../components/ui/textarea";
import { forgetPatchFailures, patchEvidence, readPatches, usePatchEvidence } from "./patchEvidence";
import { fetchCheckRecipes, type CheckRecipesProjection } from "../checkRecipesApi";
import { getPartTestRun, runPartTest, usePartTestRunsVersion } from "../partTestRuns";
import { revisionHasNoTestCommand, usePartProof, usePartProofRecord } from "../partProof";
import { hasProofFrom, sinceReviewLine } from "../sinceReview";
import { NoTestCommandLine, PartProofBadge } from "../PartProofBadge";
import { toastAiReason, useAiGate } from "../aiBudget";
import { classifyFocus, flowAction, screenLegend } from "../flowKeymap";
import { FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf } from "../FlowTopBar";
import { useReviewFlowNavigation } from "../useReviewFlowNavigation";
import { useCopyLink, useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import { buildReviewFlowPath } from "../reviewRoutes";
import { agentName, linesLabel, partName, partRoute, partSlug, partsOf } from "../overview/overviewModel";
import { setDefaultView } from "../overview/defaultView";
import { getFlowBindings, useFlowBindings } from "../settings/userSettings";
import { aiFindingsForPart, inlineNotesForPart, notesForPart, visibleReviewNotes } from "../focus/focusAnnotations";
import { FocusCode, scrollToCodeLine, type LineComment } from "../focus/FocusCode";
import { CodeViewControls } from "../codeView";
import { useSeenCoverage } from "../seen/SeenCoverageProvider";
import { QUICK_CHECK_RESUME } from "../home/resumeFor";
import { useSaveResume } from "../home/useSaveResume";
import { loadFocusDraft, saveFocusDraft } from "../focus/focusDraftPersistence";
import { counterFor, resolvePart } from "../focus/focusModel";
import { isColdLoad } from "../reviewBootLocation";
import type { Annotation, AnnotationState, MarkState, ReviewOverview, ReviewTarget } from "../types";
import { mapByModule } from "../scale/scaleTier";
import { pressedIn } from "../answerTone";
import { groupedQuick, guardContext, guardOfPart, latchRoutineSession, reasonAfterRun, quickConfirmation, quickDirectory, quickParts, quickPatchPaths, quickRemaining, type QuickGroup, type QuickGuard } from "./quickCheckModel";

export interface ReviewQuickCheckScreenProps {
  reviewRef: string;
  apiReviewId: string;
  part: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  annotations: readonly Annotation[];
  complete: boolean;
  aiAvailable: boolean;
  readOnly: boolean;
  onMark: (target: ReviewTarget, state: MarkState) => Promise<boolean | "queued">;
  /** A whole-part note, or (with `line`) an inline comment on a line or range written in the code. */
  onNote: (target: ReviewTarget, body: string, reconsideredAfterStructure: boolean, line?: LineComment) => Promise<boolean | "queued">;
  onAnnotationState?: (id: string, state: AnnotationState) => void;
  onAsk?: (target: ReviewTarget) => void;
  /** "Ask about the change" (the top bar). */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
}

/** The Quick check is a screen inside /one/<part>, not a new URL family (SPEC 3.1). */
/** How many cards or rows a list draws at once. A large change has thousands; the rest are reached by answering, which moves the window. */
const QUICK_WINDOW = 20;

/** The entries of `list` within `size` of `centre`, with their positions in the whole list. */
const isOpenPart = (part: ReviewTarget) => part.state !== "reviewed" && part.state !== "needs_changes";

function windowAround<T>(list: readonly T[], centre: number, size = QUICK_WINDOW): { item: T; index: number }[] {
  const start = Math.max(0, Math.min(centre - Math.floor(size / 2), list.length - size));
  return list.slice(start, start + size).map((item, offset) => ({ item, index: start + offset }));
}

export function ReviewQuickCheckScreen(props: ReviewQuickCheckScreenProps) {
  const { overview, apiReviewId, targets, annotations, aiAvailable, readOnly } = props;
  const aiGate = useAiGate();
  const bindings = useFlowBindings();
  const revision = overview.revision.id;
  const { goTo } = useReviewFlowNavigation(props.reviewRef);
  const seen = useSeenCoverage();
  const complete = props.complete;
  const canWrite = complete && !readOnly;
  // P11-2: parts changed since the reviewer's review are asked first, no proof before proof.
  const proofRecord = usePartProofRecord(apiReviewId, revision);
  const hasProof = useMemo(() => hasProofFrom(proofRecord), [proofRecord]);
  const quick = useMemo(() => quickParts(targets, hasProof), [targets, hasProof]);
  // Answer updates must not refetch code or temporarily move safe groups into guarded cards.
  const fileKeys = useMemo(() => [...new Set(quick.map((part) => part.path))].sort().join("\\u0000"), [quick]);
  const decisions = useMemo(() => partsOf(targets, "decision"), [targets]);
  const rules = useMemo(() => partsOf(targets, "accepted_by_rule"), [targets]);
  const dependencyFiles = new Set(["package-lock.json", "poetry.lock", "uv.lock", "pdm.lock", "Cargo.lock", "yarn.lock", "pnpm-lock.yaml", "go.sum", "Gemfile.lock", "bun.lock", "bun.lockb", "npm-shrinkwrap.json", "composer.lock", "Pipfile.lock", "Podfile.lock", "mix.lock", "gradle.lockfile", "packages.lock.json"]);
  const dependencyDecisions = decisions.filter((part) => part.kind === "dependency" || dependencyFiles.has(part.path.split("/").pop() ?? ""));
  const chosen = resolvePart(quick, props.part);
  // The part in hand: the route's part while it is unanswered, else the next unanswered one after it in the parts' order,
  // wrapping round. Answer state only (never the guards), so patches are read around it without cascading. The cards drawn and
  // the patches read follow this one anchor (quickPatchPaths).
  const anchorIndex = useMemo(() => {
    const open = (part: ReviewTarget) => part.state !== "reviewed" && part.state !== "needs_changes";
    const from = Math.max(0, chosen ? quick.indexOf(chosen) : 0);
    for (let step = 0; step < quick.length; step += 1) {
      const index = (from + step) % quick.length;
      if (open(quick[index])) return index;
    }
    return from;
  }, [quick, chosen]);
  const anchorPath = quick[anchorIndex]?.path ?? "";
  const wantedKey = useMemo(() => quickPatchPaths(quick, anchorPath).join("\\u0000"), [quick, anchorPath]);
  const patchRequested = useRef<{ revision: string; paths: Set<string> }>({ revision: "", paths: new Set() });
  // /one with no part named is still the Quick check: Home continues there.
  useSaveResume(apiReviewId, canWrite ? QUICK_CHECK_RESUME(chosen ? partSlug(chosen, targets) : "") : null, canWrite);
  // Keyed by the session id, as the Overview keys it, so both read one record.
  const evidence = usePatchEvidence(overview.session.id, revision);
  const [checks, setChecks] = useState<CheckRecipesProjection | null>(null);
  usePartTestRunsVersion();
  // P11-1: one record for the revision, read once; each row below is a map lookup (never a request or a subscription).
  const proof = usePartProof(apiReviewId, revision, { inView: quick.slice(Math.max(0, anchorIndex), Math.max(0, anchorIndex) + 40).map((part) => part.path), autoRun: canWrite });
  const noTestCommand = revisionHasNoTestCommand(proof);
  const runOf = (part: ReviewTarget) => getPartTestRun(apiReviewId, revision, part.unit_key);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [batchDone, setBatchDone] = useState<Partial<Record<QuickGroup, string[]>>>({});
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [shownGroups, setShownGroups] = useState<ReadonlySet<QuickGroup>>(new Set());
  const [noteTarget, setNoteTarget] = useState<ReviewTarget | null>(null);
  const [noteBody, setNoteBody] = useState("");
  const [noteDrafts, setNoteDrafts] = useState<Record<string, string>>({});
  const [noteSaved, setNoteSaved] = useState(0);
  const [announcement, setAnnouncement] = useState("");
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const headerRef = useRef<HTMLHeadingElement>(null);
  const pendingRef = useRef(false);

  // Read the frozen code, and never let an earlier revision's response count (the evidence is keyed by revision and a read
  // that is another revision's is recorded unusable). Reads do not decide who is in a batch: a part the server proved safe
  // stays in it whether or not its code is read, and its code is read when the batch is confirmed. A read that fails or is
  // unusable takes the part out of the batch, here and in the Overview's count. A small change reads every file, a large one
  // the files around the part in hand and a look-ahead (not a burst that grows with the change), so the cards drawn have
  // their code. Code already read is kept while the window moves; a read that failed is tried again when this opens.
  useEffect(() => {
    patchRequested.current = { revision, paths: new Set() };
    forgetPatchFailures(overview.session.id, revision);
  }, [overview.session.id, revision, fileKeys]);
  useEffect(() => {
    const requested = patchRequested.current;
    if (requested.revision !== revision) return;
    const read = patchEvidence(overview.session.id, revision).loaded;
    const missing = (wantedKey ? wantedKey.split("\\u0000") : []).filter((path) => !requested.paths.has(path) && !read.has(path));
    if (missing.length === 0) return;
    for (const path of missing) requested.paths.add(path);
    void readPatches(apiReviewId, overview.session.id, revision, missing, { inHand: anchorPath });
  }, [apiReviewId, overview.session.id, revision, fileKeys, wantedKey, anchorPath]);

  useEffect(() => {
    let live = true;
    setChecks(null);
    fetchCheckRecipes(apiReviewId, revision).then((projection) => {
      if (live && projection.revision_id === revision) setChecks(projection);
    }).catch(() => { /* A missing trusted mapping must not become a guessed Run test. */ });
    return () => { live = false; };
  }, [apiReviewId, revision]);

  useEffect(() => {
    setError("");
    setBatchDone({});
    setExpanded(new Set());
    setShownGroups(new Set());
    setNoteTarget(null);
    setAnnouncement("");
  }, [revision, apiReviewId]);

  useEffect(() => { if (!isColdLoad()) headerRef.current?.focus({ preventScroll: true }); }, [props.part, revision]);

  const humanNotes = useMemo(() => visibleReviewNotes(annotations, overview.session.id, revision), [annotations, overview.session.id, revision]);
  const findings = useMemo(() => new Map(quick.map((part) => [part.target_id,
    aiFindingsForPart(annotations, overview.session.id, revision, part, true),
  ])), [quick, annotations, overview.session.id, revision]);
  const routineIds = latchRoutineSession(overview.session.id, quick, revision);
  // The same function the Overview counts with (guardOfPart over the same inputs).
  const guardInputs = useMemo(() => guardContext({
    reviewId: overview.session.id, annotationReviewId: overview.session.id, revisionId: revision,
    parts: quick, annotations, failedPaths: evidence.failed,
  }), [overview.session.id, revision, quick, annotations, evidence.failed]);
  const guards = useMemo(() => new Map<string, QuickGuard>(quick.map((part) => [part.target_id, guardOfPart(part, guardInputs)])), [quick, guardInputs]);
  const guarded = useMemo(() => quick.filter((part) => guards.get(part.target_id)?.guarded !== false), [quick, guards]);
  // A part already answered, here or on an earlier revision, is drawn but no longer asked for.
  const guardedOpen = useMemo(() => guarded.filter(isOpenPart).length, [guarded]);
  // From 30 parts (SPEC 4.15) quick checks group by directory; the guard is the same.
  const byDirectory = complete && mapByModule(targets.length);
  const groups = useMemo(() => groupedQuick(quick, guards, byDirectory, routineIds), [quick, guards, byDirectory, routineIds]);
  // One part drives both the cards drawn and the keyboard actions: the first unanswered guarded part at or after the anchor in the
  // parts' order, wrapping round. With no route part it is `active`, so A / Looks good always mark a card that is drawn.
  const focusPart = useMemo(() => {
    for (let step = 0; step < quick.length; step += 1) {
      const part = quick[(anchorIndex + step) % quick.length];
      if (part.state !== "reviewed" && part.state !== "needs_changes" && guards.get(part.target_id)?.guarded !== false) return part;
    }
    return null;
  }, [quick, guards, anchorIndex]);
  // The cards drawn centre on that part (else the first guarded one at or after the anchor), so they are the ones whose patches are read.
  const cardCentre = useMemo(() => {
    if (focusPart) return Math.max(0, guarded.indexOf(focusPart));
    const position = new Map(quick.map((part, index) => [part.target_id, index]));
    const found = guarded.findIndex((part) => (position.get(part.target_id) ?? 0) >= anchorIndex);
    return found < 0 ? Math.max(0, guarded.length - 1) : found;
  }, [quick, guarded, anchorIndex, focusPart]);
  // What the screen draws, in one place: the cards, and each group's rows. Anything that marks a part individually (A, the
  // palette's Looks good, a note) may act only on a part in this set.
  const cardWindow = useMemo(() => windowAround(guarded, cardCentre), [guarded, cardCentre]);
  const groupRows = useMemo(() => new Map(groups.map((group) => {
    // The last answered rows in order (at most QUICK_WINDOW) stay, so a walk forward keeps its latest confirmations visible,
    // but they never push the unanswered rows out or grow the list as the reviewer walks a long group.
    const openCount = group.parts.filter(isOpenPart).length;
    let openLeft = shownGroups.has(group.name) ? openCount : Math.min(openCount, QUICK_WINDOW);
    let doneSkip = Math.max(0, group.parts.length - openCount - QUICK_WINDOW);
    const drawn = group.parts.filter((part) => (isOpenPart(part) ? openLeft-- > 0 : doneSkip-- <= 0));
    return [group.name, { drawn, openCount }] as const;
  })), [groups, shownGroups]);
  const drawnParts = useMemo(() => {
    const ids = new Set<string>(cardWindow.map(({ item }) => item.target_id));
    for (const { drawn } of groupRows.values()) for (const part of drawn) ids.add(part.target_id);
    return ids;
  }, [cardWindow, groupRows]);
  const drawnRef = useRef(drawnParts);
  drawnRef.current = drawnParts;
  // Where the reviewer is (the counter, the All files link, Copy link) may be a part that is not drawn; what a key or palette
  // action answers never is.
  const current = chosen ?? focusPart ?? quick.find(isOpenPart) ?? quick[0] ?? null;
  const candidate = chosen && isOpenPart(chosen) && drawnParts.has(chosen.target_id) ? chosen
    : focusPart ?? quick.find((part) => isOpenPart(part) && drawnParts.has(part.target_id)) ?? chosen;
  const active = candidate && drawnParts.has(candidate.target_id) ? candidate : null;
  const left = quickRemaining(quick);
  const mappingFor = useCallback((part: ReviewTarget) => checks?.revision_id === revision
    ? checks.part_checks?.find((row) => row.unit_key === part.unit_key && row.path === part.path && row.symbol === part.symbol)
    : undefined, [checks, revision]);

  const markOne = useCallback(async (part: ReviewTarget, state: MarkState) => {
    const result = await props.onMark(part, state);
    if (result === "queued") {
      setError("The answer was queued for hosted review; it has not been applied yet.");
      return "queued" as const;
    }
    if (!result) {
      setError("The answer was not saved. No confirmation was recorded.");
      return false;
    }
    return true;
  }, [props.onMark]);

  const individually = useCallback(async (part: ReviewTarget) => {
    if (!canWrite || pendingRef.current || !drawnRef.current.has(part.target_id)) return;
    pendingRef.current = true; setBusy(true); setError("");
    try {
      if (await markOne(part, "reviewed") === true) {
        setAnnouncement(`Recorded as reviewed: ${partName(part)} looks good.`);
        headerRef.current?.focus({ preventScroll: true });
      }
    } finally { pendingRef.current = false; setBusy(false); }
  }, [canWrite, markOne]);

  const batch = useCallback(async (group: { name: QuickGroup; parts: ReviewTarget[] }, undo = false) => {
    if (!canWrite || pendingRef.current) return;
    // Recheck at click time; only verified, unguarded, unchanged-revision units can be batched.
    // Undo touches only parts still marked reviewed; a part the reviewer since changed by hand keeps its answer.
    const items = undo
      ? group.parts.filter((part) => batchDone[group.name]?.includes(part.target_id) && part.state === "reviewed")
      : group.parts.filter((part) => part.state !== "reviewed" && part.state !== "needs_changes");
    if (!undo && items.some((part) => guards.get(part.target_id)?.guarded !== false)) {
      setError("Batch blocked: at least one part needs its own answer."); return;
    }
    pendingRef.current = true; setBusy(true); setError("");
    const completed: string[] = [];
    let queued = 0;
    try {
      if (!undo) {
        // The batch holds every part the server proved safe, read or not. Read the code not read yet; a part whose code
        // cannot be used leaves the batch (here and in the Overview's count) and nothing is marked.
        const read = patchEvidence(overview.session.id, revision).loaded;
        await readPatches(apiReviewId, overview.session.id, revision, [...new Set(items.map((part) => part.path))].filter((path) => !read.has(path)));
        const failed = patchEvidence(overview.session.id, revision).failed;
        const out = items.filter((part) => failed.has(part.path));
        if (out.length > 0) {
          setError(`Batch blocked: the code of ${out.length} ${out.length === 1 ? "part" : "parts"} could not be read here, so ${out.length === 1 ? "it needs" : "they need"} their own answer.`);
          return;
        }
      }
      for (const part of items) {
        const result = await markOne(part, undo ? "unreviewed" : "reviewed");
        if (result === false) break;
        // A queued mark is tracked too, so Undo can still find it once it is applied.
        if (result === "queued") queued += 1;
        completed.push(part.target_id);
      }
      setBatchDone((before) => ({
        ...before,
        [group.name]: undo
          ? (before[group.name] ?? []).filter((id) => !completed.includes(id))
          : [...new Set([...(before[group.name] ?? []), ...completed])],
      }));
      if (queued > 0) setError(`${queued} of ${items.length} changes were queued for hosted review and are not applied yet.`);
      else if (completed.length !== items.length) setError(`Only ${completed.length} of ${items.length} changes were saved. The others remain unconfirmed.`);
      else setAnnouncement(undo ? `${completed.length} quick checks undone.` : quickConfirmation(completed.map((id) => items.find((part) => part.target_id === id)!)));
    } finally { pendingRef.current = false; setBusy(false); }
  }, [apiReviewId, batchDone, canWrite, guards, markOne, overview.session.id, revision]);

  const runOne = useCallback(async (part: ReviewTarget) => {
    const mapping = mappingFor(part);
    if (!mapping || !canWrite) return;
    await runPartTest({ reviewId: apiReviewId, revisionId: revision, unitKey: part.unit_key, mapping, path: part.path });
  }, [apiReviewId, canWrite, mappingFor, revision]);

  const runGroup = useCallback(async (parts: ReviewTarget[]) => {
    if (pendingRef.current) return;
    pendingRef.current = true; setBusy(true);
    try {
      for (const part of parts) if (mappingFor(part)) await runOne(part);
    } finally { pendingRef.current = false; setBusy(false); }
  }, [mappingFor, runOne]);

  const openNote = useCallback((part: ReviewTarget) => {
    setNoteTarget(part);
    setNoteBody(noteDrafts[part.unit_key] ?? loadFocusDraft(apiReviewId, revision, part.unit_key));
  }, [noteDrafts, apiReviewId, revision]);
  const saveNote = useCallback(async () => {
    if (!noteTarget || !noteBody.trim() || !canWrite || pendingRef.current) return;
    pendingRef.current = true; setBusy(true); setError("");
    try {
      const result = await props.onNote(noteTarget, noteBody.trim(), false);
      if (result === "queued") { setError("The note was queued; the part remains unconfirmed."); return; }
      if (!result) { setError("The note was not saved. Your draft is kept."); return; }
      saveFocusDraft(apiReviewId, revision, noteTarget.unit_key, "");
      setNoteDrafts((before) => ({ ...before, [noteTarget.unit_key]: "" }));
      setNoteBody(""); setNoteTarget(null); setNoteSaved((count) => count + 1);
      setAnnouncement(`Recorded as reviewed: ${partName(noteTarget)} needs a change.`);
    } finally { pendingRef.current = false; setBusy(false); }
  }, [noteTarget, noteBody, canWrite, props.onNote, apiReviewId, revision]);

  const commentOnLine = useCallback(async (part: ReviewTarget, line: LineComment) => {
    if (!canWrite) return false;
    const result = await props.onNote(part, line.body, false, line);
    if (result === true && !line.parentId) setNoteSaved((count) => count + 1);
    return result;
  }, [canWrite, props.onNote]);

  // The Quick check keymap is the same one Focus and Overview use. Shortcuts never fire while typing.
  const actionsRef = useRef({ saveNote, closeNote: () => setNoteTarget(null), active, current, busy, individually, openNote, runOne, mappingFor, goTo, left, aiAvailable, nextUnseen: seen.nextUnseen, onAsk: props.onAsk, aiBlocked: aiGate.reason });
  actionsRef.current = { saveNote, closeNote: () => setNoteTarget(null), active, current, busy, individually, openNote, runOne, mappingFor, goTo, left, aiAvailable, nextUnseen: seen.nextUnseen, onAsk: props.onAsk, aiBlocked: aiGate.reason };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const el = document.activeElement as HTMLElement | null;
      const callbacks = actionsRef.current;
      const action = flowAction(event, {
        screen: "quickCheck",
        bindings: getFlowBindings(),
        surface: isDialogOpen() ? (document.querySelector("[data-focus-composer]") ? "composer" : "other") : "none",
        focus: el ? classifyFocus({ tagName: el.tagName, isContentEditable: el.isContentEditable, role: el.getAttribute("role") }) : "other",
        // Enter goes to Wrap up once the part in hand is answered. That touches no part, so it follows where the reviewer is (current), not the drawn part a key may answer (active).
        answered: Boolean(callbacks.current && !isOpenPart(callbacks.current)),
        aiAsk: callbacks.aiAvailable && Boolean(callbacks.onAsk),
        aiBlocked: callbacks.aiBlocked,
        testAvailable: Boolean(callbacks.active && callbacks.mappingFor(callbacks.active)),
      });
      if (!action || callbacks.busy) return;
      switch (action.intent) {
        case "looksGood": if (callbacks.active) void callbacks.individually(callbacks.active); break;
        case "needsChange": if (callbacks.active) callbacks.openNote(callbacks.active); break;
        case "runTest": if (callbacks.active) void callbacks.runOne(callbacks.active); break;
        case "ask": if (callbacks.active) callbacks.onAsk?.(callbacks.active); break;
        case "aiUnavailable": toastAiReason(action.reason ?? callbacks.aiBlocked); break;
        case "nextUnseenRisky": callbacks.nextUnseen(); break;
        case "saveNote": void callbacks.saveNote(); break;
        case "closeSurface": if (action.surface === "composer") callbacks.closeNote(); else return; break;
        case "help": setShortcutsOpen(true); break;
        case "wrapUp": case "goWrapUp": callbacks.goTo({ kind: "wrap" }, { wrapUp: true }); break;
        case "switchView": setDefaultView("all"); callbacks.goTo({ kind: "all", part: callbacks.current ? partSlug(callbacks.current, targets) : "" }); break;
        default: return;
      }
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const openLine = (index: number, line: number) => {
    const root = document.getElementById(`quick-code-${index}`);
    if (scrollToCodeLine(root, line)) root?.querySelector<HTMLElement>('[data-testid="focus-code"]')?.focus({ preventScroll: true });
    else setAnnouncement("The cited line isn't available in the frozen patch shown here.");
  };

  const fromWrap = new URLSearchParams(window.location.search).get("from") === "wrap";
  const counter = counterFor(targets, current?.target_id ?? "", complete);
  const switchToAll = () => { setDefaultView("all"); goTo({ kind: "all", part: current ? partSlug(current, targets) : "" }); };

  const copyLink = useCopyLink();
  useFlowPaletteScreen({
    reviewRef: props.reviewRef,
    apiReviewId,
    parts: partEntries(props.reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, "Quick check"),
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "quickCheck",
      part: active ? partName(active) : undefined,
      canAnswer: canWrite && !busy && Boolean(active),
      aiAsk: aiAvailable && Boolean(props.onAsk) && Boolean(active),
      view: "one",
      actions: {
        looksGood: () => { if (active) void individually(active); },
        needsChange: () => { if (active) openNote(active); },
        switchView: switchToAll,
        wrapUp: () => goTo({ kind: "wrap" }, { wrapUp: true }),
        copyLink: () => { if (current) void copyLink?.(new URL(buildReviewFlowPath(props.reviewRef, partRoute(current, targets)), window.location.href).href); },
        ask: aiGate.run(() => { if (active) props.onAsk?.(active); }),
      },
    },
  });

  return (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle="Quick check"
        title={overview.title}
        revisionNumber={overview.revision.revision_number}
        back={{ text: fromWrap ? "Wrap up" : "Overview", onClick: () => goTo(fromWrap ? { kind: "wrap" } : { kind: "overview" }) }}
        view={{ current: "one", onChange: switchToAll }}
        counter={counter}
        status={counter ? undefined : { text: "Loading all parts…" }}
        onAsk={aiAvailable ? props.onAskChange : undefined}
        askKey
        onShortcuts={() => setShortcutsOpen(true)}
        moreItems={[{ label: "All files", onSelect: switchToAll }]}
      />
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={screenLegend("quickCheck", { aiAsk: aiAvailable && Boolean(props.onAsk), testAvailable: Boolean(active && mappingFor(active)), bindings })} />
      <div role="region" className="min-w-0 flex-1" data-review-scroll-root="quick-check" aria-label="Quick check">
        <div className="flex w-full min-w-0 flex-col gap-4 px-4 py-5 pb-24 sm:px-6">
          <div className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
          <div className="max-w-3xl">
            <p className="font-mono text-tag uppercase text-muted-foreground">Quick check</p>
            <h1 ref={headerRef} tabIndex={-1} className="mt-2 text-title font-semibold focus:outline-none">
              {/* When some quick checks can still be confirmed together, the heading counts against all of them, as the tray does (SPEC 4.5 keeps "N parts left to confirm"). */}
              {guardedOpen > 0 && left > guardedOpen
                ? `${guardedOpen} of ${left} parts ${guardedOpen === 1 ? "needs its" : "need their"} own answer.`
                : guardedOpen === 1 ? "One part needs its own answer." : guardedOpen > 1 ? `${guardedOpen} parts need their own answer.` : "Anything wrong in the small stuff?"}
            </h1>
            {decisions.length === 0 && <p className="mt-2 text-label text-muted-foreground">No decisions here · {quick.length} quick checks.</p>}
            <p className="mt-2 text-label text-muted-foreground">A quick check can't be confirmed together with others when its test did not run here and the change touches a loop bound or arithmetic. Read the hint, then answer for this part.</p>
            {!complete && <p role="status" className="mt-3 text-label text-p1">Still loading parts. Batch confirmation is unavailable.</p>}
            <NoTestCommandLine proof={proof} className="mt-2" />
          </div>
          <CodeViewControls className="justify-end" />
          </div>
          {error && <p role="alert" className="rounded-md border border-p1-line bg-p1-bg p-3 text-label text-p1">{error}</p>}
          {announcement && <p role="status" className="text-label text-muted-foreground">{announcement}</p>}
          {guarded.length > 0 && <section aria-labelledby="quick-guarded-heading" className="space-y-3">
            <h2 id="quick-guarded-heading" className="text-heading font-semibold">Needs its own answer · {guardedOpen}</h2>
            {guarded.length > QUICK_WINDOW ? <p className="text-meta text-muted-foreground" data-testid="quick-window-note">Showing {QUICK_WINDOW} of {guarded.length}, around the one you are on; answering moves the window.</p> : null}
            {cardWindow.map(({ item: part, index }) => {
              const ai = aiAvailable ? findings.get(part.target_id) ?? [] : [];
              const run = runOf(part) ?? null;
              const mapping = mappingFor(part);
              const saved = notesForPart(humanNotes, part);
              const answered = part.state === "reviewed" || part.state === "needs_changes";
              const showCode = active?.target_id === part.target_id || expanded.has(part.target_id) || ai.length > 0;
              return <article key={part.target_id} className="min-w-0" data-testid="quick-guarded-card">
                <div className="min-w-0 border-l-4 border-p1-solid py-1 pl-4 sm:pl-5">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="break-all font-mono text-heading font-semibold">{partName(part)}</h3>
                    <Badge tone={answered ? "success" : "warning"}>{answered ? "Reviewed" : "Not checked yet"}</Badge>
                    <Badge tone="ai">{ai.length > 0 ? "AI hint · review yourself" : "No AI check"}</Badge>
                    <PartProofBadge proof={proof} path={part.path} showReason saidOnScreen={noTestCommand} />
                    {/* With no test command there is nothing to run: the screen's one line already says so. */}
                    {(!run || run.state === "running") && !(noTestCommand && !mapping) && <Badge tone="warning">Test did not run here</Badge>}
                    {run?.state === "done" && run.verdict === "PASS" && <Badge tone="neutral">Test passed{run.reused ? " · reused" : ""} · coverage not assumed</Badge>}
                    {run?.state === "done" && run.verdict === "FAIL" && <Badge tone="warning">Test failed</Badge>}
                  </div>
                  <p className="mt-1 break-all font-mono text-meta text-muted-foreground">{linesLabel(part)}{byDirectory && groups.some((group) => group.name === quickDirectory(part)) ? <span data-testid="quick-pulled-out"> · pulled out of the {quickDirectory(part)} group</span> : null}</p>
                  <p className="mt-3 text-label text-p1" data-testid="quick-guard-reason">{reasonAfterRun(guards.get(part.target_id)?.reason ?? "", run ?? undefined)}</p>
                  {ai.length > 0 && <div className="mt-3 space-y-2 rounded-md border border-ai-line bg-ai-bg p-3">
                    {ai.map((finding) => <div key={finding.id} className="space-y-1">
                      <p className="text-label text-ai">AI hint · {finding.body}</p>
                      <Button variant="link" size="sm" onClick={() => openLine(index, finding.start_line)}>based on {part.path} {finding.start_line}{finding.end_line > finding.start_line ? `–${finding.end_line}` : ""} · open line</Button>
                    </div>)}
                  </div>}
                  {!showCode && <Button variant="outline" size="sm" className="mt-3" onClick={() => setExpanded((old) => new Set(old).add(part.target_id))}>View code evidence</Button>}
                  {showCode && <div className="mt-3" id={`quick-code-${index}`}>
                    <FocusCode reviewRef={apiReviewId} revisionId={revision} target={part} notes={inlineNotesForPart(annotations, overview.session.id, revision, part)} aiFindings={ai} onComment={canWrite ? (line) => commentOnLine(part, line) : undefined} onAnnotationState={props.onAnnotationState} onSeen={(block, ms) => seen.reportSeen(block, ms, "dwell")}/>
                  </div>}
                  {mapping ? <p className="mt-3 text-meta text-muted-foreground">Test · {mapping.title}</p> : noTestCommand ? null : <p className="mt-3 text-meta text-muted-foreground">No test is mapped to this part</p>}
                  {mapping && <Button variant="outline" size="sm" className="mt-2" disabled={!canWrite || busy} onClick={() => void runGroup([part])}>
                    {run?.state === "running" ? <LoaderCircle className="animate-spin" size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />} Run test
                  </Button>}
                  {run && <p role="status" data-testid="quick-test-result" tabIndex={-1} className="mt-2 rounded-md border border-border p-2 text-label">{run.detail}{run.state === "done" && run.verdict === "PASS" ? " · A pass does not show the changed lines are checked." : ""}</p>}
                  {saved.map((note) => <p key={note.id} className="mt-2 whitespace-pre-wrap text-label">Saved note · {note.body}</p>)}
                  <div className="mt-4 flex flex-wrap gap-2 border-t border-border pt-3">
                    <Button variant="outline" size="sm" {...pressedIn("", part.state, "good")} disabled={!canWrite || busy} onClick={() => void individually(part)}><Check size={16} aria-hidden="true" /> Looks good</Button>
                    <Button variant="outline" size="sm" {...pressedIn("", part.state, "change")} disabled={!canWrite || busy} onClick={() => openNote(part)}><Flag size={16} aria-hidden="true" /> Needs a change</Button>
                    {aiAvailable && props.onAsk && <Button variant="ghost" size="sm" {...aiGate.attrs} onClick={aiGate.run(() => props.onAsk?.(part))}><Sparkles size={16} aria-hidden="true" /> Ask</Button>}
                  </div>
                  <p className="mt-2 text-meta text-muted-foreground">Not in a batch: answer this one yourself.</p>
                </div>
              </article>;
            })}
          </section>}
          {byDirectory && groups.length > 0 && <p className="text-label font-semibold" data-testid="quick-confirm-together">Confirm together · {groups.length} {groups.length === 1 ? "group" : "groups"}</p>}
          {groups.map((group) => {
            // Confirm together covers the parts the screen lists: all of them once "Show all" is pressed, else the first few.
            const { drawn, openCount } = groupRows.get(group.name)!;
            const unchecked = drawn.filter(isOpenPart);
            const checked = batchDone[group.name]?.filter((id) => group.parts.some((part) => part.target_id === id && part.state === "reviewed")) ?? [];
            return <section key={group.name} className="min-w-0 space-y-3" data-testid="quick-batch-group">
              <h2 className="text-heading font-semibold">{group.name} · {group.parts.length}</h2>
              {drawn.map((part) => <div key={part.target_id} className="min-w-0 rounded-lg border border-border p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0"><p className="break-all font-mono text-label">{partName(part)}</p><p className="break-all text-meta text-muted-foreground">{part.path} · {expanded.has(part.target_id) ? "viewed" : "not viewed yet"}</p>{sinceReviewLine(part) ? <p className="text-meta text-muted-foreground" data-testid="quick-since-review">{sinceReviewLine(part)}</p> : null}</div>
                  <div className="flex items-center gap-2">
                    <PartProofBadge proof={proof} path={part.path} showReason saidOnScreen={noTestCommand} />
                    <Badge tone={part.state === "reviewed" ? "success" : "neutral"}>{part.state === "reviewed" ? "Confirmed" : part.state === "needs_changes" ? "Needs a change" : "Not checked yet"}</Badge>
                    <Button variant="outline" size="sm" aria-expanded={expanded.has(part.target_id)} onClick={() => setExpanded((before) => {
                      const next = new Set(before); if (next.has(part.target_id)) next.delete(part.target_id); else next.add(part.target_id); return next;
                    })}>{expanded.has(part.target_id) ? "Hide code" : "View code"}</Button>
                  </div>
                </div>
                {expanded.has(part.target_id) && <div className="mt-3">
                  <FocusCode reviewRef={apiReviewId} revisionId={revision} target={part} notes={inlineNotesForPart(annotations, overview.session.id, revision, part)} aiFindings={aiAvailable ? findings.get(part.target_id) ?? [] : []} onComment={canWrite ? (line) => commentOnLine(part, line) : undefined} onAnnotationState={props.onAnnotationState} onSeen={(block, ms) => seen.reportSeen(block, ms, "dwell")}/>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button variant="outline" size="sm" disabled={!canWrite || busy} onClick={() => void individually(part)}>Looks good</Button>
                    <Button variant="outline" size="sm" disabled={!canWrite || busy} onClick={() => openNote(part)}>Needs a change</Button>
                  </div>
                </div>}
                {runOf(part) && <p role="status" className="mt-2 text-label text-muted-foreground" data-testid="quick-group-test-result">{partName(part)} · {runOf(part)!.detail}</p>}
              </div>)}
              {openCount > QUICK_WINDOW && !shownGroups.has(group.name) ? <div className="flex flex-wrap items-center gap-2" data-testid="quick-group-more">
                <p className="text-meta text-muted-foreground">{openCount - QUICK_WINDOW} more in this group are not listed, so Confirm together covers only the {QUICK_WINDOW} listed. Show all {openCount} to confirm the rest.</p>
                <Button variant="outline" size="sm" onClick={() => setShownGroups((before) => new Set(before).add(group.name))}>Show all {openCount}</Button>
              </div> : null}
              <p className="text-meta text-muted-foreground">Confirming a group saves each of its parts as reviewed. The saved record does not mark them as quick-checked.</p>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" disabled={!canWrite || busy || unchecked.length === 0} onClick={() => void batch({ name: group.name, parts: drawn })}><Check size={16} aria-hidden="true" /> {byDirectory ? `Confirm ${unchecked.length} together` : `Looks good · ${unchecked.length}`}</Button>
                <Button variant="outline" disabled={!canWrite || busy || !group.parts.some((part) => mappingFor(part))} onClick={() => void runGroup(group.parts)}><Play size={16} aria-hidden="true" /> Run tests for these</Button>
                {checked.length > 0 && <><span role="status" className="self-center text-label text-ok">{quickConfirmation(checked.map((id) => group.parts.find((part) => part.target_id === id)!))}</span><Button variant="ghost" disabled={!canWrite || busy} onClick={() => void batch(group, true)}>Undo</Button></>}
              </div>
            </section>;
          })}
          {dependencyDecisions.length > 0 && <section className="space-y-2 rounded-lg border border-p0-line bg-card p-4" data-testid="quick-dependency-decisions">
            <h2 className="text-heading font-semibold">Dependency decisions · {dependencyDecisions.length}</h2>
            <p className="text-label text-muted-foreground">A new dependency or an unverified lockfile is a supply-chain decision. Never batch confirmed. Package registry and maintainer details are not verified here.</p>
            {dependencyDecisions.slice(0, QUICK_WINDOW).map((part) => <div key={part.target_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border p-2">
              <div><p className="font-mono text-label">{partName(part)} · {part.path}</p><p className="text-meta text-muted-foreground">{part.answer?.reason === "new_dependency" ? "New dependency · requires a person’s review" : "Dependency change · exact package facts not available here"}</p>
                {part.reasons.filter((reason) => reason.trim()).map((reason) => <p key={reason} className="text-meta" data-testid="quick-dependency-fact">Checked in code · {reason}</p>)}
                <p className="text-meta text-muted-foreground">Not verified · no package registry lookup (published date, downloads, maintainer)</p></div>
              <Button variant="outline" onClick={() => goTo(partRoute(part, targets))}>Review decision</Button>
            </div>)}
            {dependencyDecisions.length > QUICK_WINDOW ? <p className="text-meta text-muted-foreground">{dependencyDecisions.length - QUICK_WINDOW} more dependency decisions; answer these first.</p> : null}
          </section>}
          {rules.length > 0 && <section className="space-y-2 rounded-lg border border-border bg-card p-4" data-testid="quick-rules">
            <h2 className="text-heading font-semibold">Accepted by rule · {rules.length}</h2>
            <p className="text-label text-muted-foreground">These parts were accepted by a configured rule, not by a person. They do not count as quick-checked.</p>
            {rules.slice(0, QUICK_WINDOW).map((part) => <p key={part.target_id} className="break-all text-label">{partName(part)} · {part.path} · accepted by rule “{part.answer?.rule_label || part.answer?.rule || "unknown"}”</p>)}
            {rules.length > QUICK_WINDOW ? <p className="text-meta text-muted-foreground">{rules.length - QUICK_WINDOW} more accepted by rule.</p> : null}
          </section>}
          {quick.length === 0 ? <p className="text-label text-muted-foreground">There are no quick checks in this revision.</p>
            : guarded.length === 0 && groups.length === 0 ? <p className="text-label text-muted-foreground">Nothing else needs a quick check in this change.</p> : null}
          <aside className="sticky bottom-0 z-20 flex w-full flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-card p-3 shadow-float" data-testid="quick-tray">
            <div className="text-label">
              {left === 0 ? "All quick checks confirmed" : `${left} ${left === 1 ? "part" : "parts"} left to confirm`}
              {noteSaved > 0 && <span className="ml-2 text-muted-foreground">· {noteSaved} note saved in this review</span>}
              {Object.values(noteDrafts).some((body) => body.trim()) && <Badge tone="warning" className="ml-2">Draft kept</Badge>}
            </div>
            <Button variant={left === 0 ? "solid" : "outline"} onClick={() => goTo({ kind: "wrap" }, { wrapUp: true })}>Wrap up <ChevronRight size={16} aria-hidden="true" /></Button>
          </aside>
          {left > 0 && <p className="text-meta text-muted-foreground">You may open Wrap up without confirming a quick check. It is recorded as not checked yet; nothing is sent automatically.</p>}
        </div>
      </div>
      <FlowAskOverlay>{props.askPanel}</FlowAskOverlay>
      <Dialog open={noteTarget !== null} onOpenChange={(open) => { if (!open) setNoteTarget(null); }}>
        <DialogContent aria-describedby={undefined} data-focus-composer="true" className="max-sm:bottom-0 max-sm:top-auto max-sm:translate-y-0">
          <div className="border-b border-border px-4 py-3"><DialogTitle>Note for {overview.revision.provenance_host?.trim() ? agentName(overview.revision.provenance_host) : "the agent"}</DialogTitle></div>
          <DialogBody>
            <p className="mb-3 break-all font-mono text-meta text-muted-foreground">{noteTarget ? linesLabel(noteTarget) : ""}</p>
            <Textarea autoFocus value={noteBody} aria-label="Say what should change" placeholder="Say what should change" className="min-h-32 text-copy" onChange={(event) => {
              const body = event.target.value;
              setNoteBody(body);
              if (noteTarget) {
                setNoteDrafts((before) => ({ ...before, [noteTarget.unit_key]: body }));
                saveFocusDraft(apiReviewId, revision, noteTarget.unit_key, body);
              }
            }} />
            <p className="mt-2 text-meta text-muted-foreground">Nothing is sent until Wrap up. Closing keeps your draft.</p>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setNoteTarget(null)}>Cancel</Button>
            <Button disabled={!canWrite || busy || !noteBody.trim()} onClick={() => void saveNote()}><Flag size={16} aria-hidden="true" /> Save note</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
