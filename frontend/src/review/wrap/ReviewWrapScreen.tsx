import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { WRAP_RESUME } from "../home/resumeFor";
import { useSaveResume } from "../home/useSaveResume";
import { Check, ChevronRight, Clipboard, FileText, LoaderCircle, Send, ShieldAlert } from "lucide-react";
import { useNavigate, useNavigationType } from "react-router-dom";

import { Badge } from "../../components/ui/badge";
import { Input as UiInput } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogTitle, isDialogOpen } from "../../components/ui/dialog";
import { Textarea } from "../../components/ui/textarea";
import { GitHubPreviewTabs } from "../github/GitHubPreviewTabs";
import { githubVerdictLabel, pullRequestTarget } from "../github/githubModel";
import type { FinishHandoff, FinishOptions, FinishRecordFacts } from "../FinishSheet";
import { exportFeedback, fetchFinishReview, previewGitHubPublish } from "../reviewApi";
import { buildReviewFlowPath } from "../reviewRoutes";
import { useReadingViewSwitch } from "../useReviewFlowNavigation";
import { useSeenCoverage } from "../seen/SeenCoverageProvider";
import { blocksForTarget, coverageRecordRows } from "../seen/seenModel";
import { savePendingSendToken } from "../done/pendingSendToken";
import { clearOfflineSend, createOfflineSend, saveOfflineSend } from "../done/offlineReviewSend";
import { classifyFocus, flowAction, screenLegend } from "../flowKeymap";
import { revisionName } from "../revisionLabel";
import { FlowAskOverlay, FlowShortcutsDialog, FlowTopBar, repoNameOf } from "../FlowTopBar";
import { useFlowPaletteScreen } from "../palette/FlowPalette";
import { reviewPlace } from "../palette/partEntries";
import { counterFor } from "../focus/focusModel";
import { useOnline } from "../useOnline";
import { useFlowToast } from "../flowToast";
import { partName, partRoute, plural } from "../overview/overviewModel";
import { NOT_SIGNED_YET } from "../record/recordModel";
import { PolicyEditBanner } from "../policy/PolicyEditBanner";
import { policyEditFiles, usePolicyGate } from "../policy/policyEdit";
import { countBlockedReason } from "../reviewFrictionCounts";
import { SecondStrip } from "../second/SecondStrip";
import { useSecondReviewer } from "../second/useSecondReviewer";
import { RetractedRecordCard } from "../record/RetractedRecordCard";
import type { Annotation, FeedbackExport, FeedbackPresentation, GitHubPublishPreview, ReviewClosure, ReviewOutcome, ReviewOutcomeKind, ReviewOverview, ReviewTarget } from "../types";
import {
  githubEventFor, readWrapImportance, writeWrapImportance, wrapAnswers, wrapBackendOutcome, wrapBlockers, wrapChoiceLabel,
  wrapMismatch, wrapNoteAnchor, wrapNotesFor, wrapPartStatus, wrapSummaryLine,
  type NoteImportance, type WrapChoice,
} from "./wrapModel";

type VerifiedPreview = { state: "ready"; payload: FeedbackExport } | { state: "loading" | "error"; message: string };
const IMPORTANCE: readonly { id: NoteImportance; label: string }[] = [
  { id: "must_fix", label: "Must fix" },
  { id: "suggestion", label: "Suggestion" },
  { id: "question", label: "Question" },
];

export interface ReviewWrapScreenProps {
  /** A hosting composition's merge requirements not yet met (required reviewers, required checks). Shown as information: the server still accepts an approval while they are pending. */
  hostBlockers?: readonly string[];
  /** A hosting composition's readiness panel (hosted policy and progress), drawn above the footer. */
  hostSections?: ReactNode;
  reviewRef: string;
  apiReviewId: string;
  overview: ReviewOverview;
  targets: readonly ReviewTarget[];
  annotations: readonly Annotation[];
  complete: boolean;
  readOnly: boolean;
  busy: boolean;
  recorded: ReviewOutcomeKind | null;
  currentOutcome?: ReviewOutcome | null;
  handoff: FinishHandoff;
  destinationControl?: ReactNode;
  sendError?: string;
  finishError?: string;
  /** Nothing is sent until this calls the Reader's existing outcome/delivery/finish workflow. */
  onFinish(outcome: ReviewOutcomeKind, summary: string, options: FinishOptions): void;
  /** Local server accepts this into its durable hold; hosted/copy/GitHub keep their original flow. */
  onQueue?(outcome: ReviewOutcomeKind, summary: string, feedback: FeedbackExport | null, operationId: string): Promise<import("../types").ReviewFlowSend>;
  /**
   * Each exact preview names where its notes go. The Reader takes that route, so the destination shown here is never
   * older than the preview (an agent session named by enrichment after the Reader loaded).
   */
  onFeedbackPrepared?(prepared: FeedbackExport): void;
  onDownloadRecord?: () => Promise<string>;
  record?: FinishRecordFacts;
  /** The screen Wrap up was opened from (SPEC 3.3): the back control reads "All files" after All files, else "Overview". */
  openedFrom?: "overview" | "all";
  /** "Ask about the change" (the top bar); absent when AI Ask is off. */
  onAskChange?: () => void;
  askPanel?: ReactNode;
}

/** Where a prepared export's notes go (as ReviewFeedbackPanel's feedbackRoute). */
function routeOf(prepared: FeedbackExport): FinishHandoff["kind"] {
  return prepared.delivery.kind ?? (prepared.delivery.supported ? "agent" : "export");
}

export function ReviewWrapScreen(props: ReviewWrapScreenProps) {
  const {
    reviewRef, apiReviewId, overview, targets, annotations, complete, readOnly, handoff, busy,
    currentOutcome, recorded, sendError = "", finishError = "",
  } = props;
  const navigate = useNavigate();
  const readingView = useReadingViewSwitch(reviewRef, targets);
  const preparedRef = useRef(props.onFeedbackPrepared);
  preparedRef.current = props.onFeedbackPrepared;
  // Hosted only: once the verdict is recorded and a second signature is in play, Done says where it stands (SPEC 4.16).
  const second = useSecondReviewer(apiReviewId, { watchMs: 4000 });
  const secondRefresh = second.refresh;
  useEffect(() => {
    if (recorded) void secondRefresh();
  }, [recorded, secondRefresh]);
  useEffect(() => {
    // Hosted (the second-reviewer route answered): a recorded verdict goes to Done, which says what was recorded and who
    // is told. A pull request publish has its own sheet and stays on Wrap up until it lands.
    const hosted = second.status !== null;
    if (recorded && (hosted ? handoff.kind !== "github" : false)) {
      navigate(buildReviewFlowPath(reviewRef, { kind: "done" }));
    }
  }, [recorded, second.status, handoff.kind, navigate, reviewRef]);
  // P7-3 (SPEC 4.19): a change that edits the review policy is approved only by a CODEOWNER of `.lemoncrow/**`.
  const policyFiles = policyEditFiles(overview);
  const policyGate = usePolicyGate(apiReviewId, policyFiles);
  const approvalBlocked = policyGate !== null && !policyGate.allowed;
  // A verdict the person picked before the gate answered is dropped when it turns out they may not approve.
  useEffect(() => {
    if (approvalBlocked) setPicked((current) => (current === "lgtm" || current === "lgtm_with_suggestions" ? null : current));
  }, [approvalBlocked]);
  const revisionId = overview.revision.id;
  const seen = useSeenCoverage();
  const coverageBlocks = useMemo(() => targets.flatMap((target) => blocksForTarget(target)), [targets]);
  const serverRecordedSeen = coverageBlocks.filter((block) => seen.ledger.rows[block.key]?.serverRecorded).length;
  const localSeen = coverageBlocks.filter((block) => Boolean(seen.ledger.rows[block.key])).length;
  const coverageRows = useMemo(() => coverageRecordRows(targets, seen.ledger), [targets, seen.ledger]);
  const answers = useMemo(() => wrapAnswers(targets), [targets]);
  // Home offers "Ready to wrap up" only once every part is answered; opening Wrap up early keeps the place you had.
  useSaveResume(apiReviewId, WRAP_RESUME, complete && !readOnly && answers.unanswered.length === 0 && answers.unconfirmedQuick.length === 0);
  // The note set is what the server would send now: the annotation IDs of its own preview, never a client-built list.
  const [serverNoteIds, setServerNoteIds] = useState<readonly string[] | null>(null);
  const [noteIdsError, setNoteIdsError] = useState(false);
  const [noteIdsTry, setNoteIdsTry] = useState(0);
  const annotationsFingerprint = useMemo(() => annotations.map((note) => `${note.id}:${note.updated_at ?? ""}:${note.state}`).join("\u0000"), [annotations]);
  const notes = useMemo(() => wrapNotesFor(annotations, serverNoteIds ?? []), [annotations, serverNoteIds]);
  const notesFingerprint = notes.map((note) => `${note.id}:${note.updated_at ?? ""}:${note.state}`).join("\u0000");
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [picked, setPicked] = useState<WrapChoice | null>(null);
  const [importance, setImportance] = useState<Record<string, NoteImportance>>(() => readWrapImportance(apiReviewId, revisionId));
  const [summaryText, setSummaryText] = useState("");
  const [testWithFixChecked, setTestWithFix] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [preview, setPreview] = useState<VerifiedPreview | null>(null);
  const [githubPreview, setGithubPreview] = useState<GitHubPublishPreview | null>(null);
  /** The verdict and summary the shown GitHub preview was planned for. */
  const [githubFor, setGithubFor] = useState("");
  const [githubError, setGithubError] = useState("");
  const [closure, setClosure] = useState<{ revision: string; value: ReviewClosure } | null>(null);
  const [closureError, setClosureError] = useState("");
  const flowToast = useFlowToast();
  const setToast = useCallback((text: string) => { if (text) flowToast.show({ text, tone: "alert" }); else flowToast.dismiss(); }, [flowToast.show, flowToast.dismiss]); // eslint-disable-line react-hooks/exhaustive-deps
  /** A send that failed for good: kept here, with Try again (SPEC 4.20 send failed). */
  const [sendFailed, setSendFailed] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [confirmMismatch, setConfirmMismatch] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const submittingRef = useRef(false);
  const navigationType = useNavigationType();
  // §7.1 Q2: the test request applies to local agent sessions only.
  const agentDestination = handoff.kind === "agent" || handoff.kind === "new_session";
  const testWithFix = testWithFixChecked && agentDestination;
  const modKey = typeof navigator !== "undefined" && /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent) ? "⌘↵" : "Ctrl+↵";

  // Focus the heading on in-app arrival; a cold load keeps the browser's own focus.
  useEffect(() => {
    const index = (window.history.state as { idx?: number } | null)?.idx ?? 0;
    if (navigationType !== "POP" || index > 0) headingRef.current?.focus({ preventScroll: true });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let live = true;
    setServerNoteIds(handoff.unsent > 0 ? null : []);
    setNoteIdsError(false);
    if (handoff.unsent <= 0) return () => { live = false; };
    exportFeedback(apiReviewId, { expectedRevisionId: revisionId }).then((payload) => {
      if (live && payload.revision_id === revisionId) setServerNoteIds(Object.keys(payload.annotation_versions));
      else if (live) setNoteIdsError(true);
    }).catch(() => { if (live) setNoteIdsError(true); });
    return () => { live = false; };
  }, [apiReviewId, revisionId, annotationsFingerprint, handoff.unsent, noteIdsTry]);

  useEffect(() => {
    setImportance(readWrapImportance(apiReviewId, revisionId));
    setPicked(null);
    setSummaryText("");
    setPreviewOpen(false);
    setSubmitted(false);
    setConfirmMismatch(false);
    setToast("");
    submittingRef.current = false;
  }, [apiReviewId, revisionId]);

  // Wrap is cold-loadable, so the old Finish sheet's authoritative closure must be loaded here too.
  useEffect(() => {
    let live = true;
    setClosure(null);
    setClosureError("");
    fetchFinishReview(apiReviewId).then((value) => {
      if (live) setClosure({ revision: revisionId, value });
    }).catch(() => { if (live) setClosureError("Couldn't load the current review completion facts. Sending is unavailable."); });
    return () => { live = false; };
  }, [apiReviewId, revisionId, notesFingerprint, targets]);

  const activeOutcome = currentOutcome && !currentOutcome.stale ? currentOutcome : null;
  // "Your answers" lists what is answered (and the quick checks); the decisions still open are listed once, above it.
  const unansweredIds = new Set(answers.unanswered.map((part) => part.target_id));
  const answered = [...answers.decisions, ...answers.quick].filter((part) => !unansweredIds.has(part.target_id));
  const chosen = picked ?? answers.suggested ?? (answers.unanswered.length === 0 ? activeOutcome?.outcome ?? null : null);
  // A blocked approval is never the default: the person picks Needs a change (or a comment) themselves.
  const choice = approvalBlocked && (chosen === "lgtm" || chosen === "lgtm_with_suggestions") && picked === null ? null : chosen;
  const outcomeSummary = choice === "lgtm_with_suggestions"
    ? [summaryText.trim(), "Approved with suggestions"].filter(Boolean).join(" · ") : summaryText.trim();
  const presentation: FeedbackPresentation | null = choice ? {
    verdict: choice,
    importance: Object.fromEntries(notes.filter((note) => importance[note.id]).map((note) => [note.id, importance[note.id]])),
    test_with_fix: testWithFix,
    summary: outcomeSummary,
  } : null;
  const presentationKey = presentation ? JSON.stringify(presentation) : "";

  // Never use a guessed copy of the backend delivery text as the preview.
  useEffect(() => {
    let live = true;
    setPreview(null);
    if (notes.length === 0 || handoff.unsent <= 0 || !presentation) return () => { live = false; };
    setPreview({ state: "loading", message: "Preparing exact feedback…" });
    exportFeedback(apiReviewId, { expectedRevisionId: revisionId, presentation }).then((payload) => {
      if (!live) return;
      if (payload.revision_id === revisionId) preparedRef.current?.(payload);
      setPreview(payload.revision_id === revisionId
        ? { state: "ready", payload }
        : { state: "error", message: "The feedback belongs to a different revision. Reload before sending." });
    }).catch(() => {
      if (live) setPreview({ state: "error", message: "The exact feedback preview could not be loaded. Nothing has been sent." });
    });
    return () => { live = false; };
  }, [apiReviewId, revisionId, notesFingerprint, notes.length, handoff.unsent, handoff.kind, handoff.label, presentationKey]);

  // SPEC 4.17: the review as it will appear on GitHub, planned for the verdict on screen before anything is recorded.
  // Typing a summary re-plans after a short pause; the earlier plan stays until the new one arrives, marked as updating.
  const githubKey = `${choice ?? ""}\u0000${outcomeSummary}`;
  const haveGithubPreview = useRef(false);
  haveGithubPreview.current = githubPreview !== null;
  useEffect(() => {
    let live = true;
    setGithubError("");
    if (handoff.kind !== "github") {
      setGithubPreview(null);
      setGithubFor("");
      return;
    }
    const timer = window.setTimeout(() => {
      previewGitHubPublish(apiReviewId, choice ? { outcome: wrapBackendOutcome(choice), summary: outcomeSummary } : undefined).then((next) => {
        if (!live) return;
        if (next.revision_id === revisionId && !next.stale) {
          setGithubPreview(next);
          setGithubFor(githubKey);
        } else {
          setGithubPreview(null);
          setGithubError("GitHub preview is unavailable for this revision.");
        }
      }).catch(() => { if (live) { setGithubPreview(null); setGithubError("The pull request preview could not be loaded."); } });
    }, haveGithubPreview.current ? 350 : 0);
    return () => { live = false; window.clearTimeout(timer); };
  }, [apiReviewId, revisionId, handoff.kind, githubKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!busy) submittingRef.current = false;
  }, [busy, recorded, finishError, sendError]);

  const noteSelectionsComplete = notes.every((note) => Boolean(importance[note.id]));
  const previewIds = preview?.state === "ready" ? Object.keys(preview.payload.annotation_versions) : [];
  const finalPreview = preview?.state === "ready" && preview.payload.revision_id === revisionId
    && previewIds.length === notes.length && notes.every((note) => previewIds.includes(note.id));
  const requiresSend = handoff.kind === "github" || handoff.unsent > 0 && notes.length > 0;
  const choiceMismatch = wrapMismatch(choice, importance, notes);
  const blockers = wrapBlockers({
    fullTargets: complete && closure?.revision === revisionId && !closureError,
    readOnly,
    answers,
    notes,
    importance,
    choice,
    reachable: handoff.available,
    destinationKind: handoff.kind,
    previewCurrent: !requiresSend || handoff.kind === "github" ? true : finalPreview,
  });
  if (choice === "changes_requested" && notes.length === 0 && !summaryText.trim()) {
    blockers.push("Add a note or a summary explaining what needs to change");
  }
  if (choice === "lgtm_with_suggestions" && notes.length === 0) {
    blockers.push("Approval with suggestions requires a note");
  }
  if (requiresSend && !handoff.available) blockers.push(handoff.reason || "The destination cannot receive this review");
  if (requiresSend && handoff.kind === "github" && !githubPreview) {
    blockers.push(githubError || "Checking the pull request destination");
  }
  if (handoff.unsent > 0 && (serverNoteIds === null || serverNoteIds.length !== notes.length)) {
    blockers.push(serverNoteIds === null ? (noteIdsError ? "Couldn't load the notes to send" : "Checking which notes will be sent") : "Some unsent comments were not loaded; review the feedback before sending");
  }
  const ready = blockers.length === 0 && !busy && !submitted && !readOnly && !recorded;
  const hasFeedback = requiresSend && noteSelectionsComplete;
  const noteCount = notes.length;
  const destinationName = handoff.kind === "github" ? pullRequestTarget(handoff.label) : handoff.label || "the author";
  // The server may post a different review than the verdict chosen (your own pull request, a verdict already on GitHub);
  // nothing here claims the chosen verdict reaches GitHub when the plan says otherwise. When the verdict is already on
  // GitHub this post only adds comments and GitHub keeps showing that verdict, so there is no "shows Commented" to say.
  const githubShownAs = handoff.kind === "github" && choice && githubPreview && githubFor === githubKey && !githubPreview.verdict_on_github && githubPreview.event !== githubEventFor(choice)
    ? githubVerdictLabel(githubPreview.event, choice) : "";
  const verdictLabel = choice ? wrapChoiceLabel(choice) : "No verdict yet";
  const online = useOnline();
  const sendLabel = !choice ? "Choose a verdict"
    : !online && Boolean(props.onQueue) && (handoff.kind === "agent" || handoff.kind === "new_session" || !requiresSend) ? "Send when back online"
    : hasFeedback
      ? handoff.kind === "github" ? `Post review to ${destinationName}`
        : handoff.kind === "export" ? `${choice === "lgtm" ? "Approve and copy" : "Copy"} ${plural(noteCount, "note")}`
          : choice === "lgtm" ? `Approve and send ${plural(noteCount, "note")} to ${destinationName}`
            : choice === "lgtm_with_suggestions" ? `Approve with suggestions and send ${plural(noteCount, "note")} to ${destinationName}`
              : `Send ${plural(noteCount, "note")} to ${destinationName}`
      : choice === "lgtm" ? "Record approval"
        : choice === "lgtm_with_suggestions" ? "Record approval with suggestions"
          : choice === "changes_requested" ? "Record request for changes" : "Record comment";

  // A11Y-3: the live region carries only what happens (toasts, SPEC 5.3), so it is empty when the screen opens; the
  // footer's reason is read through Send's description (aria-describedby), not announced on every change of it.
  const footerText = recorded ? "Verdict recorded" : blockers.length ? blockers.join(" · ") : `Ready to ${hasFeedback ? "send" : "record"} · nothing sent yet`;
  const toPart = (part: ReviewTarget) => {
    const path = buildReviewFlowPath(reviewRef, partRoute(part, targets));
    navigate(`${path}?from=wrap`);
  };
  const assignImportance = (noteId: string, kind: NoteImportance) => {
    setImportance((current) => {
      const next = { ...current, [noteId]: kind };
      writeWrapImportance(apiReviewId, revisionId, next);
      return next;
    });
    setToast("");
  };

  const perform = useCallback(async (confirmed = false) => {
    if (recorded || busy || submittingRef.current) return;
    if (!ready) {
      const reason = blockers[0] || (submitted ? "The last request is still being processed." : "This review is not ready to send.");
      setToast(reason);
      countBlockedReason();
      return;
    }
    if (choiceMismatch && !confirmed) {
      setConfirmMismatch(true);
      return;
    }
    if (!choice) { setToast("Choose a verdict before sending."); return; }
    submittingRef.current = true;
    setSubmitted(true);
    setToast("");
    setSendFailed("");
    try {
      // The exact preview's route is the one the notes take; the handoff the Reader passed can be older than it.
      const sendKind = requiresSend && handoff.kind !== "github" && preview?.state === "ready" ? routeOf(preview.payload) : handoff.kind;
      const queueEligible = Boolean(props.onQueue && (sendKind === "agent" || sendKind === "new_session" || !requiresSend));
      const offlineNow = typeof navigator !== "undefined" && navigator.onLine === false;
      // Offline: the last server-provided preview and closure remain available,
      // but a fresh write is impossible. Queue locally with a stable ID and
      // revalidate the whole intent on the server after reconnection.
      if (queueEligible && offlineNow && closure?.revision === revisionId && (!requiresSend || preview?.state === "ready")) {
        const intent = createOfflineSend(reviewRef, apiReviewId, revisionId, wrapBackendOutcome(choice), outcomeSummary,
          requiresSend && preview?.state === "ready" ? preview.payload : null);
        if (!saveOfflineSend(intent)) {
          setToast("Offline send could not be saved in this tab. Nothing was sent.");
          return;
        }
        navigate(buildReviewFlowPath(reviewRef, { kind: "done" }));
        return;
      }
      // A preview may have gone stale while the reviewer was deciding. A changed message is never sent as the one shown.
      if (requiresSend && handoff.kind !== "github" && preview?.state === "ready") {
        const current = await exportFeedback(apiReviewId, { expectedRevisionId: revisionId, presentation: presentation! });
        if (current.revision_id !== revisionId || current.feedback_hash !== preview.payload.feedback_hash || current.operation_id !== preview.payload.operation_id) {
          setToast("Feedback changed after the preview. Review the updated text before sending.");
          setPreview({ state: "ready", payload: current });
          return;
        }
      }
      const fresh = await fetchFinishReview(apiReviewId);
      if (fresh.status !== "open" || fresh.target_count !== closure?.value.target_count) {
        setToast("The review changed while you were on Wrap up. Reload and check the answers before recording.");
        return;
      }
      const outcome = wrapBackendOutcome(choice);
      // The backend currently has three outcomes: approval with suggestions records LGTM with notes,
      // never an invented fourth signed verdict.
      if (queueEligible && props.onQueue) {
        // Persist the intent before making a network write. If its response
        // disappears, the same operation ID can be retried idempotently.
        const proof = requiresSend && preview?.state === "ready" ? preview.payload : null;
        const intent = createOfflineSend(reviewRef, apiReviewId, revisionId, outcome, outcomeSummary, proof);
        if (!saveOfflineSend({ ...intent, attempted: true })) {
          setToast("This send could not be saved for recovery. Nothing was sent.");
          return;
        }
        try {
          const queued = await props.onQueue(outcome, outcomeSummary, proof, intent.operationId);
          clearOfflineSend(intent.operationId);
          savePendingSendToken(reviewRef, apiReviewId, queued, hasFeedback ? destinationName : "");
        } catch (reason) {
          const status = (reason as { status?: number })?.status;
          if (typeof status === "number" && status !== 502 && status !== 503 && status !== 504) {
            clearOfflineSend(intent.operationId);
            throw reason;
          }
          // No response: do not assert acceptance. The background component
          // reconciles this one ID on reconnection, never creates a fresh ID.
        }
        navigate(buildReviewFlowPath(reviewRef, { kind: "done" }));
      } else {
        // Copy-for-a-person is not reversible; GitHub uses its own human
        // confirmation. Neither route advertises a fake undo interval.
        props.onFinish(outcome, outcomeSummary, {
          send: requiresSend,
          ...(requiresSend && handoff.kind !== "github" && presentation && preview?.state === "ready"
            ? { presentation, expectedFeedbackHash: preview.payload.feedback_hash } : {}),
        });
      }
      setConfirmMismatch(false);
    } catch (error) {
      setSendFailed(error instanceof Error ? error.message : "");
    } finally {
      submittingRef.current = false;
      setSubmitted(false);
    }
  }, [recorded, busy, ready, blockers, choiceMismatch, choice, requiresSend, handoff.kind, preview, apiReviewId, revisionId, closure, outcomeSummary, presentationKey, props]);

  // ⌘K on Wrap up: Send (or Record) is the button's own function; when it is off, perform() toasts the reason.
  useFlowPaletteScreen({
    reviewRef,
    apiReviewId,
    place: reviewPlace(repoNameOf(overview), overview.title, "Wrap up"),
    openShortcuts: () => setShortcutsOpen(true),
    context: {
      screen: "wrap",
      recorded: Boolean(recorded),
      sendLabel: choice ? sendLabel : undefined,
      actions: {
        send: () => void perform(),
        changeVerdict: () => document.querySelector<HTMLElement>('[data-testid="wrap-verdict"] [role="group"] button')?.focus(),
      },
    },
  });

  const actionsRef = useRef({ perform, recorded, ready, busy });
  actionsRef.current = { perform, recorded, ready, busy };
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const node = document.activeElement as HTMLElement | null;
      const action = flowAction(event, {
        screen: "wrap", surface: isDialogOpen() ? "other" : "none",
        focus: node ? classifyFocus({ tagName: node.tagName, isContentEditable: node.isContentEditable, role: node.getAttribute("role") }) : "other",
      });
      if (action?.intent === "help") {
        event.preventDefault();
        setShortcutsOpen(true);
      } else if (action?.intent === "sendOrRecord") {
        event.preventDefault();
        if (!actionsRef.current.recorded) void actionsRef.current.perform();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const runRecordDownload = async () => {
    if (!props.onDownloadRecord) return;
    setDownloadError("");
    try { await props.onDownloadRecord(); }
    catch { setDownloadError("Could not download the signed record."); }
  };

  const openedFromAll = props.openedFrom === "all";
  const counter = counterFor(targets, "", complete);

  return (
    <>
      <FlowTopBar
        repo={repoNameOf(overview)}
        documentTitle="Wrap up"
        title={overview.title}
        revisionNumber={overview.revision.revision_number}
        back={{ text: openedFromAll ? "All files" : "Overview", onClick: () => navigate(buildReviewFlowPath(reviewRef, openedFromAll ? { kind: "all", part: "" } : { kind: "overview" })) }}
        view={readingView}
        counter={counter}
        status={counter ? undefined : { text: "Loading all parts…" }}
        onAsk={props.onAskChange}
        onShortcuts={() => setShortcutsOpen(true)}
      />
      <FlowShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} legend={screenLegend("wrap", { aiAsk: Boolean(props.onAskChange) })} />
      <div role="region" className="min-w-0 flex-1" data-review-scroll-root="wrap" aria-label="Wrap up">
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-5 px-4 py-5 pb-24 sm:px-6">
          <div>
            <p className="font-mono text-tag uppercase text-muted-foreground">Review · {revisionName(overview.revision.revision_number)}</p>
            <h1 ref={headingRef} tabIndex={-1} className="mt-2 text-title font-semibold focus:outline-none">Wrap up</h1>
            <p className="mt-2 text-label text-muted-foreground">{recorded ? "Your verdict was recorded. Check delivery status before assuming the notes were received." : answers.good + answers.needsChange > 0 ? "Here is what you decided. Nothing has been sent yet." : "Nothing decided yet. Nothing has been sent."}</p>
            {wrapSummaryLine(answers) && <p className="mt-1 text-meta text-muted-foreground" data-testid="wrap-summary">{wrapSummaryLine(answers)}</p>}
          </div>

          <RetractedRecordCard reviewId={apiReviewId} revisionId={revisionId} revisionNumber={overview.revision.revision_number} repo={repoNameOf(overview)} />

          {recorded && <section data-testid="wrap-recorded" className="rounded-lg border border-ok-line bg-ok-bg p-4">
            <h2 className="flex items-center gap-2 font-semibold"><Check size={20} aria-hidden="true" /> {recorded === "lgtm" ? "Approval recorded" : recorded === "changes_requested" ? "Changes requested" : "Comment recorded"}</h2>
            <p className="mt-1 text-label">Your verdict has been recorded on {revisionName(overview.revision.revision_number)}. This did not commit or merge anything.</p>
            {props.onDownloadRecord && recorded !== "comment" && <Button className="mt-3" variant="outline" onClick={() => void runRecordDownload()}>Download signed record</Button>}
            {downloadError && <p role="alert">{downloadError}</p>}
            <Button className="mt-3 ml-2" variant="outline" onClick={() => navigate(buildReviewFlowPath(reviewRef, { kind: "overview" }))}>Back to Overview</Button>
          </section>}

          {(sendError || finishError || sendFailed) && <div role="alert" data-testid="wrap-send-failed" className="rounded-md border border-p0-line bg-p0-bg p-3 text-label">
            <p>Couldn't send · kept here · your notes and answers are safe.</p>
            {(sendError || finishError || (/^\d*$/.test(sendFailed) ? "" : sendFailed)) && <p>{sendError || finishError || sendFailed}</p>}
            <Button className="mt-2" variant="outline" disabled={busy} onClick={() => void perform()}>Try again</Button>
          </div>}
                    {flowToast.element}
          {noteIdsError && serverNoteIds === null && <div role="alert" className="flex flex-wrap items-center gap-2 text-label text-p1">
            <span>Couldn't load the notes to send. Nothing has been sent.</span>
            <Button variant="outline" onClick={() => setNoteIdsTry((count) => count + 1)}>Try again</Button>
          </div>}
          {closureError && <p role="alert" className="text-label text-p1">{closureError}</p>}
          {!closure && !closureError && <p className="text-label text-muted-foreground">Checking current completion facts…</p>}

          {answers.unanswered.length > 0 && <section className="rounded-lg border border-p0-line bg-card p-4" aria-labelledby="wrap-unanswered" data-testid="wrap-unanswered">
            <h2 id="wrap-unanswered" className="flex items-center gap-2 text-heading font-semibold"><ShieldAlert size={20} className="text-p1" aria-hidden="true" /> {plural(answers.unanswered.length, "decision")} still need your answer</h2>
            <p className="mt-2 text-label text-muted-foreground">Nothing is sent, and no verdict is suggested, until every decision has an answer.</p>
            <ul className="mt-3 space-y-2">{answers.unanswered.map((part) => <li key={part.target_id} className="flex items-center justify-between gap-2 border-t border-border pt-2">
              <span className="min-w-0 break-all font-mono text-label">{partName(part)} · {part.path}</span>
              <Button variant="outline" onClick={() => toPart(part)}>Answer <ChevronRight size={16} aria-hidden="true" /></Button>
            </li>)}</ul>
          </section>}

          <section aria-labelledby="wrap-answers-heading" className="overflow-hidden rounded-lg border border-border bg-card">
            <h2 id="wrap-answers-heading" className="px-4 py-3 text-heading font-semibold">Your answers</h2>
            <div className="divide-y divide-border">
              {answered.map((part) => <div key={part.target_id} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3" data-testid="wrap-answer-row">
                <div className="min-w-0 flex-1">
                  <p className="break-all font-mono text-label">{partName(part)}</p>
                  <p className="break-all text-meta text-muted-foreground">{part.path} · {part.answer?.category === "quick_check" ? "quick check" : "decision"}</p>
                </div>
                <Badge tone={part.state === "needs_changes" && !part.changed_since_mark ? "warning" : part.state === "reviewed" && !part.changed_since_mark ? "success" : "neutral"}>{wrapPartStatus(part)}</Badge>
                <Button variant="ghost" onClick={() => toPart(part)}>{wrapPartStatus(part) === "Not answered" || wrapPartStatus(part) === "Not checked yet" ? "Answer" : "Edit"}</Button>
              </div>)}
              {!answers.decisions.length && !answers.quick.length ? <p className="px-4 py-3 text-label text-muted-foreground">No parts require an individual decision.</p>
                : answered.length === 0 ? <p className="px-4 py-3 text-label text-muted-foreground" data-testid="wrap-no-answers">No answers yet. The decisions above come first.</p> : null}
            </div>
            {answers.accepted.length > 0 && <p className="border-t border-border px-4 py-3 text-meta text-muted-foreground">{plural(answers.accepted.length, "part")} accepted by a configured rule · not counted as human answers.</p>}
            {answers.unconfirmedQuick.length > 0 && <p className="border-t border-border px-4 py-3 text-meta text-muted-foreground">{plural(answers.unconfirmedQuick.length, "quick check")} not checked yet. They are recorded that way if you finish.</p>}
          </section>

          <section aria-labelledby="wrap-notes-heading" className="rounded-lg border border-border bg-card p-4" data-testid="wrap-notes">
            <h2 id="wrap-notes-heading" className="text-heading font-semibold">{plural(noteCount, "note")} for {destinationName}</h2>
            {noteCount === 0 ? <p className="mt-2 text-label text-muted-foreground">No notes to send. Your verdict can be recorded without notifying a coding agent.</p> : <p className="mt-2 text-meta text-muted-foreground">{handoff.kind === "github" ? "Choose each note's importance. GitHub comments leave it out." : "Choose each note's importance. It goes out with the note."}</p>}
            <div className="mt-3 divide-y divide-border">
              {notes.map((note) => <div key={note.id} className="space-y-3 py-4 first:pt-1" data-testid="wrap-note">
                <p className="break-all font-mono text-meta text-muted-foreground">{wrapNoteAnchor(note)}</p>
                <blockquote className="whitespace-pre-wrap border-l-2 border-border pl-3 text-label">{note.body}</blockquote>
                {note.reconsidered_after_structure && note.structure_revision_id === revisionId && <Badge tone="warning">Answer changed after seeing structure</Badge>}
                <div className="space-y-2">
                  <p className="text-label font-semibold">Importance {importance[note.id] ? `· Set by you · ${IMPORTANCE.find((item) => item.id === importance[note.id])?.label}` : "· not set"}</p>
                  <div role="group" aria-label={`Importance for ${wrapNoteAnchor(note)}`} className="flex flex-wrap gap-2">
                    {IMPORTANCE.map((item) => <Button key={item.id} variant={importance[note.id] === item.id ? "solid" : "outline"} aria-pressed={importance[note.id] === item.id} onClick={() => assignImportance(note.id, item.id)}>{item.label}</Button>)}
                  </div>
                </div>
              </div>)}
            </div>
          </section>

          <PolicyEditBanner files={policyFiles} gate={policyGate} reviewId={apiReviewId} />

          <SecondStrip apiReviewId={apiReviewId} choice={choice} status={second.status} />

          <section aria-labelledby="wrap-destination-heading" className="rounded-lg border border-border bg-card p-4" data-testid="wrap-destination">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="wrap-destination-heading" className="text-heading font-semibold">Destination</h2>
              {props.destinationControl}
            </div>
            <p className="mt-2 text-label">{handoff.kind === "github" ? `Post review to ${destinationName}` : handoff.kind === "export" ? "Copy for a person" : `Send to ${destinationName}`}</p>
            <p className="mt-1 text-meta text-muted-foreground">{handoff.kind === "export"
              ? "Copies the server's exact feedback. You pass it on yourself."
              : handoff.kind === "github" ? "Final GitHub review is shown in a separate confirmation before posting."
                : "Goes only to the coding session this review came from."}</p>
            {!handoff.available && noteCount > 0 && <p role="alert" className="mt-2 text-label text-p1">{handoff.reason || "This destination cannot receive feedback."}</p>}
            {noteCount > 0 && agentDestination && <label className="mt-3 flex items-center gap-3 text-label">
              <UiInput unstyled type="checkbox" checked={testWithFixChecked} onChange={(event) => setTestWithFix(event.target.checked)} className="h-5 w-5 accent-current" />
              Ask for a test with each fix
            </label>}
            {testWithFix && <p className="mt-1 text-meta text-p1">The outgoing message will ask for a test on each fix note, but not on a Question.</p>}
            <div className="mt-4">
              <Button variant="outline" onClick={() => setPreviewOpen(true)}><FileText size={16} aria-hidden="true" /> Preview exactly what is sent</Button>
              {preview?.state === "error" && <p role="alert" className="mt-2 text-label text-p1">{preview.message}</p>}
            </div>
          </section>

          <section aria-labelledby="wrap-verdict-heading" className="rounded-lg border border-border bg-card p-4" data-testid="wrap-verdict">
            <h2 id="wrap-verdict-heading" className="text-heading font-semibold">Your verdict</h2>
            {/* SPEC 4.8: the reason from your answers, until you pick a verdict; then only "Chosen by you". */}
            <p className="mt-2 text-label text-muted-foreground" data-testid="wrap-based-on">{picked ? "Chosen by you" : answers.reason}</p>
            <div role="group" aria-label="Choose your verdict" className="mt-3 flex flex-wrap gap-2">
              {(["lgtm", "changes_requested"] as const).map((kind) =>
                <Button key={kind} variant={choice === kind ? "solid" : "outline"} aria-pressed={choice === kind} disabled={kind === "lgtm" && approvalBlocked} title={kind === "lgtm" && approvalBlocked ? policyGate?.reason : undefined} onClick={() => { setPicked(kind); setToast(""); }}>{wrapChoiceLabel(kind)}</Button>,
              )}
            </div>
            <details className="mt-3" data-testid="wrap-more-verdicts"><summary className="cursor-pointer text-label underline underline-offset-2 pointer-coarse:flex pointer-coarse:items-center">More verdicts</summary>
              <div className="mt-3 flex flex-wrap gap-2">
                {(["lgtm_with_suggestions", "comment"] as const).map((kind) =>
                  <Button key={kind} variant={choice === kind ? "solid" : "outline"} aria-pressed={choice === kind} disabled={kind === "lgtm_with_suggestions" && approvalBlocked} onClick={() => { setPicked(kind); setToast(""); }}>{wrapChoiceLabel(kind)}</Button>,
                )}
              </div>
              <p className="mt-2 text-meta text-muted-foreground">Approve with suggestions records an approval with your notes, not a separate fourth backend verdict.</p>
            </details>
            <label htmlFor="wrap-summary-input" className="mt-4 block text-label">Your summary (optional, unless requesting changes without notes)</label>
            <Textarea id="wrap-summary-input" className="mt-2 min-h-20 text-copy" value={summaryText} onChange={(event) => setSummaryText(event.target.value.slice(0, 16384))} placeholder="In your own words…" />
            {choiceMismatch && <p role="alert" data-testid="wrap-mismatch" className="mt-3 rounded-md border border-p1-line bg-p1-bg p-3 text-label">{choiceMismatch}</p>}
            <p className="mt-3 text-meta text-muted-foreground">Finishing records your verdict on {revisionName(overview.revision.revision_number)}. It never commits or merges.</p>
          </section>

          <section className="rounded-lg border border-border bg-card p-4" aria-label="What happens next">
            <h2 className="text-heading font-semibold">What happens next</h2>
            <ol className="mt-3 space-y-2 text-label">
              <li>1. {choice ? `Your ${wrapChoiceLabel(choice).toLowerCase()} verdict is recorded on this revision.${githubShownAs ? ` GitHub will show the review as ${githubShownAs}.` : ""}` : "Choose a verdict."}</li>
              <li>2. {noteCount > 0 ? `${plural(noteCount, "note")} ${noteCount === 1 ? "is" : "are"} ${handoff.kind === "export" ? "copied for you" : handoff.kind === "github" ? "posted on the pull request after you confirm" : `sent to ${destinationName}`}.` : "No notes are sent."}</li>
              <li>3. Nothing is committed or merged by Wrap up.</li>
            </ol>
          </section>
          <details className="rounded-lg border border-border bg-card p-4">
            <summary className="cursor-pointer text-label font-semibold pointer-coarse:flex pointer-coarse:items-center" data-testid="wrap-record-line">{`Signed record · ${revisionName(overview.revision.revision_number)} · ${plural(answers.decisions.length - answers.unanswered.length, "decision")} answered · Details`}</summary>
            <div className="mt-3 space-y-2 text-meta text-muted-foreground" data-testid="wrap-record-details">
              <p>{revisionName(overview.revision.revision_number)}{props.record?.headSha ? ` · ${props.record.headSha.slice(0, 12)}` : " · commit not available"}{props.record?.dirty ? " · includes captured uncommitted changes" : ""}</p>
              <p>{props.record ? `${props.record.targets.filter((part) => part.reviewed || part.needsChanges).length} of ${props.record.targets.length} parts inspected` : "Inspecting exact review facts…"}</p>
              <p data-testid="wrap-seen-coverage">Seen code (reviewer-reported): {localSeen} of {coverageBlocks.length} changed blocks seen · {serverRecordedSeen} recorded on the server. Seeing code never marks it approved.</p>
              <p data-testid="wrap-coverage-rows">Coverage · {coverageRows.coverage.length ? coverageRows.coverage.join(" · ") : "no risky parts"}</p>
              {coverageRows.skipped.map((line) => <p key={line} data-testid="wrap-skipped-line">{line}</p>)}
              <p>{seen.omitted ? seen.omissionRecorded ? `Skipped changed code was disclosed to the server for ${revisionName(overview.revision.revision_number)}.` : (seen.ledger.omission?.recordedCount ?? 0) > 0 ? `Only part of the skipped changed code reached the server (${seen.ledger.omission?.recordedCount} of ${seen.ledger.omission?.blockKeys.length} blocks); the rest is not in a signed record.` : "Skipped changed code was kept only in this browser; do not claim it in a signed record." : "No skipped-code disclosure recorded yet."}</p>
              <p>{props.record?.passedChecks.length ? props.record.passedChecks.map((check) => `${check.label} · ${check.source}`).join(" · ") : "No verified passing checks recorded here"}</p>
              <p>{props.record?.openConditions.length ? props.record.openConditions.map((condition) => condition.label).join(" · ") : "No additional outstanding checks listed"}</p>
              <p>{props.record?.notMeasured ? `${props.record.notMeasured} facts could not be checked` : "No unchecked-fact omissions listed"}</p>
              {props.record?.reconsideredNotes?.map((note) => <p key={note.id}>Answer changed after seeing structure · {note.path} · {note.body}</p>)}
              <p data-testid="wrap-not-signed">{NOT_SIGNED_YET}</p>
            </div>
          </details>
          {props.hostSections || props.hostBlockers?.length ? (
            <section className="rounded-lg border border-border bg-card p-3" aria-label="Before you approve" data-testid="wrap-host-sections">
              {props.hostBlockers?.length ? (
                <div data-testid="wrap-host-requirements">
                  <p className="text-label font-semibold">Not met yet · your approval is still accepted</p>
                  <ul className="mt-1 list-disc pl-5 text-label text-muted-foreground">
                    {props.hostBlockers.map((label) => <li key={label}>{label}</li>)}
                  </ul>
                </div>
              ) : null}
              {props.hostSections}
            </section>
          ) : null}
          <div className="sticky bottom-0 z-20 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card p-3" data-testid="wrap-footer">
            <div className="min-w-0 flex-1">
              <p className="text-label font-semibold">{verdictLabel}{githubShownAs ? ` · GitHub shows ${githubShownAs}` : ""}</p>
              <p className="text-meta text-muted-foreground" id="wrap-footer-reason">{footerText}</p>
            </div>
            <div className="flex items-center gap-2">
              <span aria-hidden="true" className="hidden rounded border border-border px-1.5 py-1 font-mono text-meta text-muted-foreground md:inline">{modKey}</span>
              <Button variant={ready ? "solid" : "outline"} disabled={!ready} aria-describedby="wrap-footer-reason" onClick={() => void perform()} data-testid="wrap-send">
                {busy || submitted ? <LoaderCircle size={16} className="animate-spin" aria-hidden="true" /> : hasFeedback ? <Send size={16} aria-hidden="true" /> : <Check size={16} aria-hidden="true" />}
                {busy || submitted ? "Recording…" : sendLabel}
              </Button>
            </div>
          </div>
        </div>
      </div>
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent aria-describedby={undefined} className="max-w-2xl">
          <div className="border-b border-border px-4 py-3"><DialogTitle>Preview exactly what is sent</DialogTitle></div>
          <DialogBody>
            {handoff.kind === "github" ? <>
              <GitHubPreviewTabs preview={githubPreview} problem={githubError} choice={choice} updating={Boolean(githubPreview) && githubFor !== githubKey} title={overview.title} />
              <p className="mt-3 text-meta text-muted-foreground">Nothing is posted yet. Posting asks you to confirm once more, against GitHub's current diff.</p>
            </> : noteCount > 0 ? preview?.state === "ready" ? <>
              <p className="text-label text-muted-foreground">Exactly the server-prepared feedback, including your note importance and test requests.</p>
              <pre data-testid="wrap-exact-preview" className="mt-3 max-h-[55vh] overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-background p-3 text-meta">{preview.payload.markdown}</pre>
              <p className="mt-2 text-meta text-muted-foreground">Your verdict is recorded separately from this feedback text.</p>
            </> : <p role="status">{preview?.message || "Loading feedback…"}</p>
              : <p className="text-label">No notes will be sent. The verdict is recorded separately.</p>}
          </DialogBody>
          <DialogFooter>
            {preview?.state === "ready" && noteCount > 0 && <Button variant="outline" onClick={() => void navigator.clipboard.writeText(preview.payload.markdown).then(() => setToast("Copied the exact feedback preview."), () => setToast("Could not copy the preview."))}><Clipboard size={16} aria-hidden="true" /> Copy preview</Button>}
            <Button onClick={() => setPreviewOpen(false)}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={confirmMismatch} onOpenChange={setConfirmMismatch}>
        <DialogContent aria-describedby={undefined}>
          <div className="border-b border-border px-4 py-3"><DialogTitle>Confirm your verdict</DialogTitle></div>
          <DialogBody><p className="text-label">{choiceMismatch}</p><p className="mt-2 text-meta text-muted-foreground">This is your judgment, not an AI decision.</p></DialogBody>
          <DialogFooter><Button variant="outline" onClick={() => setConfirmMismatch(false)}>Go back</Button><Button onClick={() => { setConfirmMismatch(false); void perform(true); }}>Confirm {verdictLabel}</Button></DialogFooter>
        </DialogContent>
      </Dialog>
      <FlowAskOverlay>{props.askPanel}</FlowAskOverlay>
    </>
  );
}
