import { useEffect, useMemo, useRef, useState } from "react";
import { Check, FileDiff, ShieldAlert } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Textarea } from "../../components/ui/textarea";
import { isQueuedWrite } from "../hostedQueue";
import { exportFeedback, fetchHistoricalAnnotations, fetchReviewOutcome, fetchReviewVerification, fetchRevisionComparison,
  postAnnotation, postMark, postReviewOutcome, sourceCompareHref } from "../reviewApi";
import { buildReviewFlowPath } from "../reviewRoutes";
import { toastAiReason, useAiGate } from "../aiBudget";
import { classifyFocus, flowAction, keyHint, screenLegend } from "../flowKeymap";
import { getFlowBindings, useFlowBindings } from "../settings/userSettings";
import { FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf } from "../FlowTopBar";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { partEntries, reviewPlace } from "../palette/partEntries";
import { openFlowSurface } from "../openSurface";
import { useOnline } from "../useOnline";
import { guardedQueueSend } from "../done/guardedSend";
import { SignedRecordLine } from "../record/SignedRecordLine";
import { readWrapImportance, wrapAnswers, wrapBlockers, wrapNotesFor, writeWrapImportance, type NoteImportance } from "../wrap/wrapModel";
import type { Annotation, FeedbackExport, FeedbackPresentation, ReviewFlowSend, ReviewOutcome, ReviewOutcomeKind, ReviewOverview, ReviewRevisionComparison, ReviewTarget, ReviewVerificationProjection, RevisionInfo } from "../types";
import { answeredHeadline, backCounter, confirmedAnswer, pairedChecks, passOnBothChecks, returnedParts } from "./welcomeBackModel";
import { SinceRevisionDiff } from "./SinceRevisionDiff";
import { agentName, partName, plural } from "../overview/overviewModel";

interface Props {
  reviewRef: string;
  apiReviewId: string;
  revision: number;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  readOnly: boolean;
  onDownloadRecord?(): Promise<string>;
  /** Use the Reader's authoritative write path so Wrap up sees the new answer immediately. */
  onMark?(target: ReviewTarget): Promise<boolean | "queued">;
  onKeepOpen?(target: ReviewTarget, body: string): Promise<boolean | "queued">;
  /** The Reader's notes, to find the ones a send would carry. */
  annotations?: readonly Annotation[];
  /** All parts are loaded; the same condition Wrap up needs before it lets a send through. */
  complete?: boolean;
  /** Where the notes would go. Only an agent session can send with Undo. */
  handoff?: { kind: "agent" | "new_session" | "export" | "github"; label: string; available: boolean; reason: string };
  /** The server-held, undoable send (local Review only). */
  onQueue?(outcome: ReviewOutcomeKind, summary: string, feedback: FeedbackExport | null, operationId: string): Promise<ReviewFlowSend>;
  /** The Compare reader exists (local Review only): offer "Compare with Revision N". */
  canCompare?: boolean;
  /** "Ask about the change" (the top bar); absent when AI Ask is off. */
  onAskChange?: () => void;
  askPanel?: React.ReactNode;
}

/** The Compare reader's source spec for a stored revision: its own `rr/` ref, else `rr/<id>`. */
function revisionSpec(revision: RevisionInfo): string {
  return revision.ref?.startsWith("rr/") ? revision.ref : `rr/${revision.id}`;
}

type Loaded = {
  comparison: ReviewRevisionComparison;
  previous: Annotation[];
  before: ReviewVerificationProjection | null;
  after: ReviewVerificationProjection | null;
  outcome: ReviewOutcome | null;
};

export function ReviewWelcomeBackScreen({
  reviewRef, apiReviewId, revision, overview, targets, readOnly, onDownloadRecord, onMark, onKeepOpen, canCompare = false,
  annotations = [], complete = true, handoff, onQueue, onAskChange, askPanel,
}: Props) {
  const ai = useAiGate();
  const bindings = useFlowBindings();
  const navigate = useNavigate();
  const location = useLocation();
  const online = useOnline();
  const latest = overview.revision.revision_number + 1;
  const current = revision === latest && revision >= 2;
  const [data, setData] = useState<Loaded | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [answers, setAnswers] = useState<Record<string, "fixed" | "change">>({});
  const [editing, setEditing] = useState<string | null>(null);
  const [note, setNote] = useState("Still not right: ");
  const [approved, setApproved] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [sendBusy, setSendBusy] = useState(false);
  const [recordResult, setRecordResult] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const focusedRef = useRef(false);
  const revisionId = overview.revision.id;

  useEffect(() => {
    if (!current) { setLoading(false); return; }
    let live = true;
    setLoading(true); setData(null); setError(""); setAnswers({}); setApproved(false);
    const beforeNumber = revision - 2; // One-based route -> zero-based stored revision.
    const afterNumber = revision - 1;
    void Promise.all([
      fetchRevisionComparison(apiReviewId, beforeNumber, afterNumber),
      fetchHistoricalAnnotations(apiReviewId, beforeNumber),
      fetchReviewOutcome(apiReviewId),
    ]).then(async ([comparison, previous, outcome]) => {
      const [before, after] = await Promise.all([
        fetchReviewVerification(apiReviewId, comparison.from_revision.id).catch(() => null),
        fetchReviewVerification(apiReviewId, comparison.to_revision.id).catch(() => null),
      ]);
      if (!live) return;
      if (comparison.to_revision.id !== revisionId || comparison.from_revision.id === revisionId) {
        setError("The comparison no longer matches this revision. Reload the review before judging.");
        return;
      }
      setData({ comparison, previous: previous.annotations, before, after, outcome: outcome.current });
      setApproved(outcome.current?.revision_id === revisionId && outcome.current?.outcome === "lgtm" && !outcome.current.stale);
    }).catch((reason: unknown) => {
      if (live) setError(reason instanceof Error ? reason.message : "Could not compare the two revisions.");
    }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [apiReviewId, revision, revisionId, current]);

  // Focus the heading when arriving from another screen, once; a cold load leaves focus where the browser put it.
  useEffect(() => { focusedRef.current = false; }, [revision]);
  useEffect(() => {
    if (location.key === "default" || loading || focusedRef.current) return;
    focusedRef.current = true;
    headingRef.current?.focus();
  }, [location.key, loading, revision]);

  const rows = useMemo(() => data
    ? returnedParts(targets, data.previous, data.comparison)
    : { returned: [], untouched: [], unmatched: [] }, [targets, data]);
  const allAnswers = useMemo(() => Object.fromEntries(rows.returned.map((row) => [
    row.target.target_id, answers[row.target.target_id] ??
      confirmedAnswer(row.target, revisionId),
  ])), [answers, revisionId, rows.returned]);
  const unresolved = rows.returned.filter((row) => !allAnswers[row.target.target_id]);
  const requests = rows.returned.filter((row) => allAnswers[row.target.target_id] === "change");
  const gateOpen = data?.comparison.code_available === true && !readOnly
    && !busy && rows.untouched.length === 0 && rows.unmatched.length === 0 && unresolved.length === 0 && rows.returned.length > 0;
  const failPass = useMemo(() => pairedChecks(data?.before ?? null, data?.after ?? null), [data]);
  const passBoth = useMemo(() => passOnBothChecks(data?.before ?? null, data?.after ?? null), [data]);
  const noteRows = rows.returned.filter((row) => row.kind === "note");
  const extraRows = rows.returned.filter((row) => row.kind === "extra");
  const notTouched = rows.untouched.length + rows.unmatched.length;
  const agent = agentName(overview.revision.provenance_host);
  const agentDestination = Boolean(onQueue && handoff && (handoff.kind === "agent" || handoff.kind === "new_session"));
  const wantSend = gateOpen && requests.length > 0 && agentDestination;
  // Which notes go is the server's own answer (its preview), never a list built here; the same rule as Wrap up.
  const [serverNoteIds, setServerNoteIds] = useState<readonly string[] | null>(null);
  const [importance, setImportance] = useState<Record<string, NoteImportance>>(() => readWrapImportance(apiReviewId, overview.revision.id));
  const [preview, setPreview] = useState<{ state: "ready"; payload: FeedbackExport } | { state: "loading" | "error"; message: string } | null>(null);
  const annotationsFingerprint = useMemo(() => annotations.map((note) => `${note.id}:${note.updated_at ?? ""}:${note.state}`).join("\u0000"), [annotations]);
  useEffect(() => {
    if (!wantSend) { setServerNoteIds(null); return; }
    let live = true;
    exportFeedback(apiReviewId, { expectedRevisionId: revisionId }).then((payload) => {
      if (live && payload.revision_id === revisionId) setServerNoteIds(Object.keys(payload.annotation_versions));
    }).catch(() => { if (live) setServerNoteIds(null); });
    return () => { live = false; };
  }, [apiReviewId, revisionId, wantSend, annotationsFingerprint]);
  const sendNotes = useMemo(() => wrapNotesFor(annotations, serverNoteIds ?? []), [annotations, serverNoteIds]);
  const importanceDone = sendNotes.length > 0 && sendNotes.every((note) => Boolean(importance[note.id]));
  const presentation: FeedbackPresentation = useMemo(() => ({
    verdict: "changes_requested", test_with_fix: false, summary: "",
    importance: Object.fromEntries(sendNotes.filter((note) => importance[note.id]).map((note) => [note.id, importance[note.id]])),
  }), [sendNotes, importance]);
  const presentationKey = JSON.stringify(presentation);
  useEffect(() => {
    setPreview(null);
    if (!wantSend || !importanceDone) return;
    let live = true;
    setPreview({ state: "loading", message: "Preparing exact feedback…" });
    exportFeedback(apiReviewId, { expectedRevisionId: revisionId, presentation }).then((payload) => {
      if (live) setPreview(payload.revision_id === revisionId ? { state: "ready", payload } : { state: "error", message: "The feedback belongs to a different revision. Reload before sending." });
    }).catch(() => { if (live) setPreview({ state: "error", message: "The exact feedback preview could not be loaded. Nothing has been sent." }); });
    return () => { live = false; };
  }, [apiReviewId, revisionId, wantSend, importanceDone, presentationKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const previewIds = preview?.state === "ready" ? Object.keys(preview.payload.annotation_versions) : [];
  const exactPreview = preview?.state === "ready" && previewIds.length === sendNotes.length && sendNotes.every((note) => previewIds.includes(note.id));
  const sendBlockers = useMemo(() => {
    const issues = wrapBlockers({
      fullTargets: complete, readOnly, answers: wrapAnswers(targets), notes: sendNotes, importance,
      choice: "changes_requested", reachable: Boolean(handoff?.available), destinationKind: handoff?.kind ?? "export", previewCurrent: exactPreview,
    });
    if (!agentDestination) issues.push("No coding agent session can take these notes from here. Open Wrap up to choose where they go.");
    else if (serverNoteIds === null) issues.push("Checking which notes will be sent");
    else if (sendNotes.length !== serverNoteIds.length || sendNotes.length === 0) issues.push("Some unsent comments were not loaded; review the feedback before sending");
    if (!online) issues.push("You're offline. Reconnect to send");
    return issues;
  }, [complete, readOnly, targets, sendNotes, importance, handoff, agentDestination, exactPreview, serverNoteIds, online]);
  const assignImportance = (noteId: string, kind: NoteImportance) => setImportance((current) => {
    const next = { ...current, [noteId]: kind };
    writeWrapImportance(apiReviewId, overview.revision.id, next);
    return next;
  });
  const approvedView = approved && gateOpen && requests.length === 0;
  // Approving changes the screen in place: the heading takes focus and the page returns to the top (SPEC 5.3). Only the
  // reviewer's own approval does this (the ref is set where it is recorded); an already approved revision opened or
  // reloaded keeps the browser's focus (never on first load).
  const focusAfterApproval = useRef(false);
  useEffect(() => {
    if (!approvedView || !focusAfterApproval.current) return;
    focusAfterApproval.current = false;
    window.scrollTo?.(0, 0);
    headingRef.current?.focus({ preventScroll: true });
  }, [approvedView]);

  const mark = async (target: ReviewTarget) => {
    if (!current || !data || readOnly || busy || approvedView) return;
    setBusy(target.target_id); setError("");
    try {
      if (onMark) {
        if (await onMark(target) !== true) throw new Error("The answer was not confirmed locally. Check the Review status before continuing.");
      } else {
        const result = await postMark(apiReviewId, target.unit_key, "reviewed", revisionId);
        if (isQueuedWrite(result)) throw new Error("The answer was queued for hosted Review, not recorded here. Open the hosted review.");
        if (result.downgraded) throw new Error(result.note || "The server did not accept the answer.");
      }
      setAnswers((before) => ({ ...before, [target.target_id]: "fixed" }));
      setEditing(null); setApproved(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not record this answer.");
    } finally { setBusy(""); }
  };

  const keepOpen = async (target: ReviewTarget) => {
    const body = note.trim();
    if (!current || !data || readOnly || busy || body === "" || body === "Still not right:") return;
    setBusy(target.target_id); setError("");
    try {
      if (onKeepOpen) {
        if (await onKeepOpen(target, body) !== true) throw new Error("The note was not confirmed locally. Check the Review status before continuing.");
      } else {
        const result = await postAnnotation(apiReviewId, {
          path: target.path, start_line: 0, end_line: 0, file_level: true,
          side: "additions", body, kind: "request_change", target_unit_key: target.unit_key, mark_target: true,
        }, revisionId);
        if (isQueuedWrite(result)) throw new Error("The note was queued for hosted Review, not recorded here. Open the hosted review.");
      }
      setAnswers((before) => ({ ...before, [target.target_id]: "change" }));
      setEditing(null); setNote("Still not right: "); setApproved(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save your note.");
    } finally { setBusy(""); }
  };

  const sendChanges = async () => {
    if (!gateOpen || requests.length === 0 || sendBlockers.length > 0 || sendBusy || !onQueue || !handoff || preview?.state !== "ready") return;
    setSendBusy(true); setError("");
    try {
      await guardedQueueSend({
        reviewRef, apiReviewId, revisionId, outcome: "changes_requested", summary: "", presentation,
        shown: preview.payload, destinationLabel: handoff.label, onQueue,
      });
      navigate(buildReviewFlowPath(reviewRef, { kind: "done" }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not schedule the send. Nothing was sent.");
    } finally { setSendBusy(false); }
  };

  const approve = async () => {
    if (!gateOpen || requests.length !== 0 || approvedView) return;
    setBusy("approve"); setError("");
    try {
      // This records a verdict on the *current* revision only. It never merges or messages an agent.
      const result = await postReviewOutcome(apiReviewId, "lgtm", "Revision " + revision + " approved after reviewing returned parts", revisionId);
      if (isQueuedWrite(result)) throw new Error("Approval was queued on hosted Review; it has not been recorded here.");
      if (!result.current || result.current.revision_id !== revisionId || result.current.outcome !== "lgtm") {
        throw new Error("The server did not confirm approval on this revision.");
      }
      focusAfterApproval.current = true;
      setApproved(true);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not record approval.");
    } finally { setBusy(""); }
  };

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const node = document.activeElement as HTMLElement | null;
      const focus = node ? classifyFocus({ tagName: node.tagName, isContentEditable: node.isContentEditable, role: node.getAttribute("role") }) : "other";
      const action = flowAction(event, { screen: "welcomeBack", bindings: getFlowBindings(), surface: openFlowSurface(), focus, aiAsk: Boolean(onAskChange), aiBlocked: ai.reason });
      if (action?.intent === "help") { event.preventDefault(); setShortcutsOpen(true); return; }
      if (action?.intent === "ask" && onAskChange) { event.preventDefault(); onAskChange(); return; }
      if (action?.intent === "aiUnavailable") { event.preventDefault(); toastAiReason(action.reason ?? ai.reason); return; }
      if (!current || editing || busy || approvedView) return;
      const next = unresolved[0];
      if (action?.intent === "looksGood" && next) { event.preventDefault(); void mark(next.target); }
      if (action?.intent === "needsChange" && next) { event.preventDefault(); setEditing(next.target.target_id); }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [current, editing, busy, approvedView, unresolved, onAskChange, ai.reason]);

  const firstOpen = unresolved[0];
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    parts: partEntries(reviewRef, targets, "one"),
    place: reviewPlace(repoNameOf(overview), overview.title, "Welcome back"),
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "welcomeBack",
      part: current && !approvedView && firstOpen ? partName(firstOpen.target) : undefined,
      canAnswer: Boolean(current) && !editing && !busy && !approvedView && !readOnly && Boolean(firstOpen),
      aiAsk: Boolean(onAskChange),
      actions: {
        looksGood: () => { if (firstOpen) void mark(firstOpen.target); },
        needsChange: () => { if (firstOpen) setEditing(firstOpen.target.target_id); },
        ask: ai.run(onAskChange),
      },
    },
  });

  const home = () => navigate("/reviews");
  const latestPath = buildReviewFlowPath(reviewRef, { kind: "rev", revision: latest });
  const openWrap = () => navigate(buildReviewFlowPath(reviewRef, { kind: "wrap" }));
  const answeredCount = rows.returned.length - unresolved.length;
  // The sub line already says how many notes came back, so no "Came back" tile repeats it (review-21.3).
  const tiles = [
    { label: "Not from your notes", count: extraRows.length, tone: "text-p2" },
    { label: "Not touched", count: notTouched, tone: "text-p1" },
  ].filter((tile) => tile.count > 0);
  const subLine = [
    noteRows.length > 0 ? `${plural(noteRows.length, "note")} came back changed.` : "",
    extraRows.length > 0 ? `${plural(extraRows.length, "change")} ${extraRows.length === 1 ? "wasn't" : "weren't"} from your notes.` : "",
  ].filter(Boolean).join(" ");

  return <>
    <FlowTopBar
      repo={repoNameOf(overview)}
      documentTitle={approvedView ? "Approved" : `Revision ${revision}`}
      title={overview.title}
      revisionNumber={overview.revision.revision_number}
      back={{ text: "Reviews", onClick: home }}
      counter={data ? backCounter(targets, rows.returned.map((row) => row.target), allAnswers, approvedView ? "" : firstOpen?.target.target_id ?? "", complete) : null}
      onAsk={onAskChange}
      askKey
      onShortcuts={() => setShortcutsOpen(true)}
    />
    <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={screenLegend("welcomeBack", { aiAsk: Boolean(onAskChange), bindings })} />
    <div className="min-w-0 flex-1" data-review-scroll-root="welcome-back">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-5 px-4 py-6 pb-28" data-testid="review-welcome-back">
        {!current ? <>
          <h1 tabIndex={-1} ref={headingRef} className="text-title font-semibold">Revision {revision} · read-only</h1>
          <p className="text-label text-muted-foreground">Judging happens on the latest revision. Your earlier record stays with its own revision.</p>
          <Button variant="brand" onClick={() => navigate(latestPath)} className="self-start">Back to latest</Button>
        </> : <>
          <section className="space-y-2">
            {/* No "Revision 2 · Round 2" eyebrow: the top bar names the revision, and the round was always the same number (review-21.3). */}
            <h1 ref={headingRef} tabIndex={-1} className="text-title font-semibold" data-testid="back-title">{approvedView ? "Revision " + revision + " approved" : data ? answeredHeadline(agent, noteRows.length) : "Revision " + revision}</h1>
            <p className="text-label text-muted-foreground">{approvedView ? "Nothing was merged. Your approval is recorded." : subLine}</p>
          </section>
          {/* Approved: the outcome and what to do next come first; the banner and answer buttons are for a revision still open. */}
          {data && approvedView ? <section className="rounded-lg border border-ok-line bg-ok-bg p-4" data-testid="back-approved">
            <SignedRecordLine reviewId={apiReviewId} revisionId={overview.revision.id} revisionNumber={overview.revision.revision_number} onDownloadUnsigned={onDownloadRecord} onOpenWrap={openWrap} />
            <h2 className="mt-3 text-label font-semibold">What next for this change</h2>
            <p className="mt-1 text-label">{agent} does not know yet. LemonCrow never merges for you.</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button variant="outline" onClick={() => void navigator.clipboard.writeText("Revision " + revision + " approved in LemonCrow. Nothing was merged.").then(() => setRecordResult(`Copied. Paste it into your ${agent === "The agent" ? "agent" : agent} session; nothing was sent for you.`)).catch(() => setRecordResult("Clipboard access unavailable."))}>{agent === "The agent" ? "Copy approval" : `Copy approval for ${agent}`}</Button>
              <Button variant="brand" onClick={home}>Back to your reviews</Button>
            </div>
            {recordResult && <p role="status" className="mt-2 text-meta">{recordResult}</p>}
          </section> : <div className="rounded-lg border border-p2-line bg-p2-bg p-3 text-label" role="note" data-testid="back-banner">
            Your Revision {revision - 1} record stays on Revision {revision - 1}. Revision {revision} needs its own signed record.
          </div>}
          {loading && <p role="status" className="text-label">Loading revision comparison and previous notes…</p>}
          {error && <p role="alert" className="rounded-lg border border-p1-line bg-p1-bg p-3 text-label">{error}</p>}
          {data && <>
            {tiles.length > 0 && <section className="grid gap-2 sm:grid-cols-3" aria-label="Revision summary">
              {tiles.map((tile) => <div key={tile.label} className="rounded-lg border border-border bg-card p-3"><div className={"text-heading font-semibold " + tile.tone}>{tile.count}</div><div className="mt-1 font-mono text-tag text-muted-foreground">{tile.label}</div></div>)}
            </section>}
            {/* The whole change between the two revisions, in the Compare reader (review-22.1): no other screen links to it. */}
            {canCompare && data.comparison.from_revision.id && data.comparison.to_revision.id && <Button variant="outline" size="sm" className="self-start" data-testid="back-compare-link"
              onClick={() => navigate(sourceCompareHref(revisionSpec(data.comparison.from_revision), revisionSpec(data.comparison.to_revision), `r/${reviewRef.replace(/^r\//, "")}`))}>
              Compare with Revision {revision - 1}
            </Button>}
            {!data.comparison.code_available && <p role="alert" className="rounded-lg border border-p1-line bg-p1-bg p-3 text-label">Code comparison is unavailable: {data.comparison.code_unavailable_reason || "no verified comparison"}. Approval is disabled.</p>}
            {failPass.length > 0 && <section aria-label="Tests that fail, then pass" className="rounded-lg border border-border bg-card p-4">
              <h2 className="text-heading font-semibold">Tests that fail, then pass</h2>
              <p className="mt-1 text-meta text-muted-foreground">Recorded against both revisions. They are not tied to a particular note, so your answer is still needed.</p>
              {failPass.map((check) => <p key={check.name} className="mt-2 font-mono text-meta" data-testid="back-fail-pass">{check.name} · Fails on Revision {revision - 1} · passes on Revision {revision}</p>)}
            </section>}
            {passBoth.length > 0 && <section aria-label="Test doesn't prove it" className="rounded-lg border border-p1-line bg-p1-bg p-4" data-testid="back-no-proof">
              <h2 className="text-heading font-semibold">Test doesn't prove it</h2>
              {passBoth.map((check) => <p key={check.name} className="mt-2 font-mono text-meta">{check.name} · Passes on Revision {revision - 1} too · it doesn't prove the fix</p>)}
              <p className="mt-2 text-label">Is it fixed? Your answer is needed.</p>
            </section>}
            <section className="space-y-3" aria-label="Returned parts">
              <h2 className="text-heading font-semibold">{noteRows.length > 0 ? `Your notes · ${noteRows.length}` : "Changed parts"}{extraRows.length > 0 ? ` · Also changed · ${extraRows.length}` : ""}</h2>
              {rows.returned.length === 0 && <p className="text-label text-muted-foreground">No individually verified changed parts to confirm. Open the code comparison; approval is not offered here.</p>}
              {rows.returned.map(({ target, notes, kind }) => {
                const answer = allAnswers[target.target_id];
                const file = data.comparison.code_files.find((item) => item.path === target.path);
                const active = editing === target.target_id;
                return <section key={target.target_id} className="overflow-hidden rounded-lg border border-border bg-card" aria-label={partName(target)} data-testid="back-part">
                  <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
                    <FileDiff size={16} aria-hidden="true" />
                    <h3 className="min-w-0 flex-1 break-all font-mono text-label font-semibold">{partName(target)}</h3>
                    {/* A note row's quote already says "Your note"; only a change that is not from your notes needs a badge (review-21.3). */}
                    {kind !== "note" && <Badge tone="p2">Not from your notes</Badge>}
                    {answer && <Badge tone={answer === "fixed" ? "success" : "warning"}>{answer === "fixed" ? "Confirmed by you" : "Still open"}</Badge>}
                  </div>
                  <div className="space-y-3 p-4">
                    <p className="break-all font-mono text-meta text-muted-foreground">{target.path}</p>
                    {notes.map((item) => <blockquote key={item.id} className="border-l-2 border-p1 pl-3 text-label"><span className="font-mono text-tag text-muted-foreground">Your note · You · Revision {revision - 1}</span><p className="mt-1 whitespace-pre-wrap">{item.body}</p></blockquote>)}
                    {kind === "extra" && <p className="text-label font-semibold">Answer this before approving.</p>}
                    {file && <div>
                      <p className="mb-2 text-meta text-muted-foreground">Since Revision {revision - 1} · the whole file's changes, which may include other parts</p>
                      <SinceRevisionDiff path={target.path} patch={file.patch} label={"Changes in " + target.path + " since Revision " + (revision - 1)} />
                    </div>}
                    {!file && <p role="note" className="text-meta text-muted-foreground">No per-file patch available here. Use the full comparison to inspect the change.</p>}
                    <p className="flex items-start gap-1 text-meta text-muted-foreground" data-untrusted="commit"><ShieldAlert size={14} className="mt-0.5 shrink-0" aria-hidden="true" /> The agent's commit message was not used as evidence</p>
                    {kind === "note" && <p className="text-meta text-muted-foreground">Not verified · no test is tied to this note. Your judgment is needed.</p>}
                    {active && <div className="space-y-2">
                      <label htmlFor={"back-note-" + target.target_id} className="text-label font-semibold">What is still wrong?</label>
                      <Textarea id={"back-note-" + target.target_id} value={note} onChange={(event) => setNote(event.target.value)} rows={3} />
                      <div className="flex flex-wrap gap-2">
                        <Button variant="outline" disabled={Boolean(busy) || note.trim() === "" || note.trim() === "Still not right:"} onClick={() => void keepOpen(target)}>Keep open</Button>
                        <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
                      </div>
                    </div>}
                    {!active && !approvedView && <div className="flex flex-wrap gap-2">
                      <Button variant={answer === "fixed" ? "success" : "outline"} disabled={readOnly || Boolean(busy) || approvedView} onClick={() => void mark(target)}><Check size={16} aria-hidden="true" /> {kind === "note" ? "Looks fixed" : "Looks good"} <span className="font-mono text-meta pointer-coarse:hidden">{keyHint("looksGood", bindings)}</span></Button>
                      <Button variant={answer === "change" ? "warning" : "outline"} disabled={readOnly || Boolean(busy) || approvedView} onClick={() => { setEditing(target.target_id); setNote("Still not right: "); }}>{kind === "note" ? "Still needs a change" : "Needs a change"} <span className="font-mono text-meta pointer-coarse:hidden">{keyHint("needsChange", bindings)}</span></Button>
                    </div>}
                  </div>
                </section>;
              })}
            </section>
            {notTouched > 0 && <section className="rounded-lg border border-p1-line bg-p1-bg p-4" role="alert">
              <h2 className="text-heading font-semibold">Earlier notes still need attention</h2>
              <p className="mt-1 text-label">An unchanged or unmatched request for changes cannot silently become approved. Inspect those notes in the comparison before continuing.</p>
              {[...rows.untouched, ...rows.unmatched].map((item) => <p key={item.id} className="mt-2 text-label">{item.path}: {item.body}</p>)}
            </section>}
            {approvedView ? null : <>
            {wantSend && sendNotes.length > 0 && <section className="rounded-lg border border-border bg-card p-4" aria-labelledby="back-send-notes" data-testid="back-send-notes">
              <h2 id="back-send-notes" className="text-heading font-semibold">{plural(sendNotes.length, "note")} for {handoff?.label}</h2>
              <p className="mt-1 text-meta text-muted-foreground">These are the notes that will be sent, nothing else. Set how important each one is.</p>
              <ul className="mt-3 divide-y divide-border">
                {sendNotes.map((note) => <li key={note.id} className="space-y-2 py-3 first:pt-0" data-testid="back-send-note">
                  <p className="font-mono text-meta text-muted-foreground">{note.path}</p>
                  <p className="whitespace-pre-wrap text-label">{note.body}</p>
                  <div role="group" aria-label={"Importance of the note on " + note.path} className="flex flex-wrap gap-2">
                    {([["must_fix", "Must fix"], ["suggestion", "Suggestion"], ["question", "Question"]] as const).map(([id, label]) =>
                      <Button key={id} variant={importance[note.id] === id ? "solid" : "outline"} aria-pressed={importance[note.id] === id} onClick={() => assignImportance(note.id, id)}>{label}</Button>)}
                  </div>
                </li>)}
              </ul>
              {preview?.state === "ready" && <div className="mt-3"><p className="font-mono text-tag text-muted-foreground">Exactly what is sent</p>
                <pre tabIndex={0} role="region" aria-label="Exactly what is sent" data-testid="back-exact-preview" className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-background p-3 text-meta">{preview.payload.markdown}</pre></div>}
              {preview && preview.state !== "ready" && <p role="status" className="mt-3 text-meta text-muted-foreground">{preview.message}</p>}
            </section>}
            <footer className="sticky bottom-0 z-20 flex flex-wrap items-center justify-between gap-3 border-t border-border bg-background py-3" data-testid="back-footer">
              <div className="min-w-0">
                <p className="text-label">{unresolved.length > 0 ? answeredCount + " of " + rows.returned.length + " answered" : requests.length > 0 ? "All answered · Request changes again" : "All answered"}</p>
                <p id="back-footer-reason" className="text-meta text-muted-foreground">{gateOpen && requests.length > 0 && sendBlockers.length > 0 ? sendBlockers[0] : "Records your verdict on Revision " + revision + ". Never merges."}</p>
              </div>
              {gateOpen && requests.length === 0 && <Button variant="brand" disabled={Boolean(busy)} onClick={() => void approve()}>Approve revision {revision}</Button>}
              {gateOpen && requests.length > 0 && <Button variant="warning" data-testid="back-send-changes" aria-describedby="back-footer-reason" disabled={sendBlockers.length > 0 || sendBusy} onClick={() => void sendChanges()}>Send {sendNotes.length > 0 ? plural(sendNotes.length, "note") : "notes"} &amp; request changes</Button>}
              {gateOpen && requests.length > 0 && sendBlockers.length > 0 && <Button variant="ghost" onClick={openWrap}>Open Wrap up</Button>}
              {!gateOpen && <Button variant="outline" onClick={openWrap}>Open Wrap up</Button>}
            </footer></>}
          </>}
        </>}
      </div>
    </div>
    <FlowAskOverlay>{askPanel}</FlowAskOverlay>
  </>;
}
