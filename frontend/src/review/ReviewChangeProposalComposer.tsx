import { Button as UiButton } from "../components/ui/button";
import { Textarea as UiTextarea } from "../components/ui/textarea";
import { Check, CircleAlert, FilePenLine, LoaderCircle, Maximize2, Minimize2, PencilLine, X } from "lucide-react";
import { Suspense, lazy, useState } from "react";

import type { ReviewChangeProposal, ReviewProposalSelection } from "./types";

// CodeMirror (~350 KB) is only needed once a reviewer opens an edit.
const EditableCodeEditor = lazy(() => import("./EditableCodeEditor"));

function proposalState(proposal: ReviewChangeProposal): { label: string; tone: string; detail: string } {
  if (proposal.state === "conflicted") {
    return {
      label: "Source moved",
      tone: "border-amber-900/60 bg-amber-950/20 text-amber-300",
      detail: "The working source no longer matches this proposal.",
    };
  }
  if (proposal.result_revision_id) {
    return {
      label: "Captured",
      tone: "border-emerald-900/60 bg-emerald-950/15 text-emerald-300",
      detail: "Captured in an immutable Snapshot.",
    };
  }
  if (proposal.state === "applied") {
    return {
      label: "Awaiting review update",
      tone: "border-sky-900/60 bg-sky-950/15 text-sky-300",
      detail: "Applied to the working source. Update the review to capture it as an immutable Snapshot.",
    };
  }
  if (!proposal.can_apply) {
    // The server refuses to apply it here: the Review has advanced past its base
    // Snapshot, or this Reader has no local source editing.
    return {
      label: "Saved proposal",
      tone: "border-border bg-muted/30 text-muted-foreground",
      detail: "Saved as a review patch. It can only be applied from the latest Snapshot with local source editing.",
    };
  }
  return {
    label: "Ready to apply",
    tone: "border-violet-900/60 bg-violet-950/15 text-violet-300",
    detail: "Saved against this exact Snapshot and source identity.",
  };
}

function PatchPreview({ patch }: { patch: string }) {
  return (
    <pre className="review-proposal-patch" aria-label="Proposal patch">
      {patch.split("\n").map((line, index) => {
        const tone = line.startsWith("+") && !line.startsWith("+++")
          ? "review-proposal-patch-add"
          : line.startsWith("-") && !line.startsWith("---")
            ? "review-proposal-patch-delete"
            : line.startsWith("@@")
              ? "review-proposal-patch-hunk"
              : "review-proposal-patch-context";
        return <span key={`${index}:${line}`} className={tone}>{line || " "}</span>;
      })}
    </pre>
  );
}

interface ReviewChangeProposalComposerProps {
  selection: ReviewProposalSelection | null;
  proposal: ReviewChangeProposal | null;
  loading: boolean;
  busy: boolean;
  directEdit?: boolean;
  quickEdit?: boolean;
  allowApply?: boolean;
  theme?: "light" | "dark";
  replacementText: string;
  intent: string;
  onReplacementChange(value: string): void;
  onIntentChange(value: string): void;
  onCreate(): void;
  onCreateAndApply(): void;
  onApply(): void;
  onClose(): void;
}

export default function ReviewChangeProposalComposer({
  selection,
  proposal,
  loading,
  busy,
  directEdit = false,
  quickEdit = false,
  allowApply = false,
  theme = "dark",
  replacementText,
  intent,
  onReplacementChange,
  onIntentChange,
  onCreate,
  onCreateAndApply,
  onApply,
  onClose,
}: ReviewChangeProposalComposerProps) {
  const [expanded, setExpanded] = useState(false);
  const canSubmit = Boolean(selection && !busy && !loading && replacementText !== selection.text);
  const save = () => { if (canSubmit) onCreate(); };
  const apply = () => { if (canSubmit) onCreateAndApply(); };
  const expandControl = selection && !proposal ? (
    <UiButton variant="ghost" size="compact" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
      {expanded ? <Minimize2 size={12} aria-hidden="true" /> : <Maximize2 size={12} aria-hidden="true" />}
      {expanded ? "Compact editor" : "Expand editor"}
    </UiButton>
  ) : null;
  const state = proposal ? proposalState(proposal) : null;
  const location = selection
    ? `${selection.path}:${selection.start_line}${selection.end_line !== selection.start_line ? `-${selection.end_line}` : ""}`
    : proposal
      ? `${proposal.path}:${proposal.start_line}${proposal.end_line !== proposal.start_line ? `-${proposal.end_line}` : ""}`
      : "";
  if (quickEdit && selection && (!proposal || proposal.state !== "conflicted")) {
    return (
      <section
        className="review-composer review-source-composer review-source-quick"
        aria-label="Quick source edit"
        aria-busy={busy || loading}
      >
        <div className="review-quick-edit-heading">
          <FilePenLine size={12} className="text-sky-300" aria-hidden="true" />
          <span className="font-medium text-neutral-300">Quick edit</span>
          {location && <span className="review-source-location" title={location}>{location}</span>}
          {proposal && state && <span className={`review-source-state ${state.tone}`}>{state.label}</span>}
          <span className="flex-1" />
          {expandControl}
          <UiButton variant="ghost" size="icon" type="button" onClick={onClose} aria-label="Close quick edit">
            <X size={12} aria-hidden="true" />
          </UiButton>
        </div>

        {!proposal ? (
          <>
            <Suspense fallback={<div className="p-3 text-label text-muted-foreground" role="status">Loading editor…</div>}>
              <EditableCodeEditor
                path={selection.path}
                value={replacementText}
                theme={theme}
                ariaLabel="Quick edit replacement code"
                startLine={selection.start_line}
                minLines={expanded ? 18 : 6}
                maxLines={expanded ? 32 : 18}
                readOnly={busy || loading}
                autoFocus
                onChange={onReplacementChange}
                onCancel={onClose}
                onSubmit={save}
                onSubmitAndApply={allowApply ? apply : undefined}
              />
            </Suspense>
            <div className="review-quick-edit-footer">
              <span className="review-composer-shortcut"><kbd>Ctrl/⌘</kbd>+<kbd>Enter</kbd> suggest{allowApply ? " · Shift to apply" : ""}</span>
              <UiButton variant="ghost" size="compact" className="h-6 px-2 text-caption" type="button" onClick={onClose}>Cancel</UiButton>
              <UiButton size="compact" type="button" disabled={!canSubmit} onClick={save}>
                {busy ? <LoaderCircle size={11} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <PencilLine size={11} aria-hidden="true" />}
                Suggest
              </UiButton>
              {allowApply && (
                <UiButton variant="default" size="compact" type="button" disabled={!canSubmit} onClick={apply}>
                  {busy ? <LoaderCircle size={11} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <FilePenLine size={11} aria-hidden="true" />}
                  Apply
                </UiButton>
              )}
            </div>
          </>
        ) : (
          <div className="review-quick-edit-footer">
            <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">{state?.detail}</span>
            {allowApply && proposal.can_apply && (
              <UiButton variant="default" size="compact" className="h-6 px-2 text-caption" type="button" disabled={busy} onClick={onApply}>
                {busy ? <LoaderCircle size={11} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <FilePenLine size={11} aria-hidden="true" />}
                Apply
              </UiButton>
            )}
            <UiButton variant="ghost" size="compact" className="h-6 px-2 text-caption" type="button" onClick={onClose}>Close</UiButton>
          </div>
        )}
      </section>
    );
  }

  return (
    <section
      className={`review-composer review-source-composer ${expanded ? "review-source-expanded" : ""} ${directEdit ? "review-source-composer-edit" : "review-source-composer-proposal"}`}
      aria-label="Source change proposal"
      aria-busy={busy || loading}
    >
      <div className="review-composer-heading review-source-composer-heading">
        {directEdit
          ? <FilePenLine size={15} className="text-sky-300" aria-hidden="true" />
          : <PencilLine size={15} className="text-violet-300" aria-hidden="true" />}
        <span>{directEdit ? "Edit source" : "Suggested change"}</span>
        {location && <span className="review-source-location" title={location}>{location}</span>}
        {state && <span className={`review-source-state ${state.tone}`}>{state.label}</span>}
        {!directEdit && !proposal && (
          <span className="review-source-safety">Patch only · source unchanged</span>
        )}
        <span className="flex-1" />
        {expandControl}
        <UiButton variant="ghost" size="icon" type="button" onClick={onClose} aria-label="Close source editor">
          <X size={14} aria-hidden="true" />
        </UiButton>
      </div>

      {loading && !selection && (
        <div className="flex items-center gap-2 px-1 py-3 text-label text-muted-foreground">
          <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          Loading exact reviewed source…
        </div>
      )}

      {selection && !proposal && (
        <>
          <p className="review-source-explainer">
            {directEdit
              ? "This writes through LemonCrow’s guarded source-edit path only after you click Apply."
              : "Shape the replacement below. Saving creates a review patch only; applying it is a separate explicit action."}
          </p>
          <div className="review-source-edit-grid">
            <div className="review-source-pane">
              <div className="review-source-pane-label">Current source</div>
              <pre className="review-source-original">{selection.text}</pre>
            </div>
            <div className="review-source-pane">
              <div className={`review-source-pane-label ${directEdit ? "text-sky-300/80" : "text-violet-300/80"}`}>{directEdit ? "Replace with" : "Suggested replacement"}</div>
              <Suspense fallback={<div className="p-3 text-label text-muted-foreground" role="status">Loading editor…</div>}>
                <EditableCodeEditor
                  path={selection.path}
                  value={replacementText}
                  theme={theme}
                  ariaLabel="Replacement code"
                  startLine={selection.start_line}
                  minLines={expanded ? 18 : 8}
                  maxLines={expanded ? 32 : 24}
                  readOnly={busy || loading}
                  autoFocus
                  onChange={onReplacementChange}
                  onCancel={onClose}
                  onSubmit={directEdit ? apply : save}
                  onSubmitAndApply={directEdit || allowApply ? apply : undefined}
                />
              </Suspense>
            </div>
          </div>
          <UiTextarea
            uiSize="xs"
            aria-label="Proposal intent"
            value={intent}
            onChange={(event) => onIntentChange(event.currentTarget.value)}
            rows={2}
            placeholder={directEdit ? "Why are you changing this? Optional." : "Why is this suggestion better? Optional."}
            className="review-source-intent focus-visible:ring-0 focus-visible:border-ring"
          />
          <div className="review-composer-footer">
            <span className="review-composer-shortcut">Exact reviewed source · stale writes are blocked</span>
            <UiButton
              variant="default"
              size="xs"
              className="px-2.5"
              type="button"
              disabled={!canSubmit}
              onClick={directEdit ? apply : save}
            >
              {busy ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : directEdit ? <FilePenLine size={14} aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
              {busy ? (directEdit ? "Applying…" : "Saving…") : directEdit ? "Apply to source" : "Save suggestion"}
            </UiButton>
          </div>
        </>
      )}

      {proposal && state && (
        <>
          <div className="review-source-preview-heading">
            <span>{directEdit ? "Patch preview" : "Suggested patch"}</span>
            <span>based on Snapshot {proposal.base_revision_number}</span>
          </div>
          <PatchPreview patch={proposal.patch} />
          {proposal.conflict_reason && (
            <div className="review-source-conflict" role="alert">
              <CircleAlert size={14} className="shrink-0" aria-hidden="true" />
              <div>
                <div className="font-medium">This patch is stale</div>
                <div className="mt-0.5 text-amber-300/75">{proposal.conflict_reason}</div>
                <div className="mt-0.5 text-amber-300/55">Update the Review, then select the current lines and create a fresh edit.</div>
              </div>
            </div>
          )}
          <div className="review-composer-footer">
            <span className="review-composer-shortcut">{state.detail}</span>
            {proposal.can_apply && (
              <UiButton variant="default" size="xs" className="px-2.5" type="button" disabled={busy} onClick={onApply}>
                {busy ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <FilePenLine size={14} aria-hidden="true" />}
                {busy ? "Applying…" : directEdit ? "Apply to source" : "Apply suggestion"}
              </UiButton>
            )}
          </div>
        </>
      )}
    </section>
  );
}
