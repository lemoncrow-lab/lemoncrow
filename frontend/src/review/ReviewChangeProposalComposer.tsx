import { Check, CircleAlert, FilePenLine, LoaderCircle, Sparkles, X } from "lucide-react";

import type { ReviewChangeProposal, ReviewProposalSelection } from "./types";

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
      detail: "Captured in an immutable Review revision.",
    };
  }
  if (proposal.state === "applied") {
    return {
      label: "Awaiting review update",
      tone: "border-sky-900/60 bg-sky-950/15 text-sky-300",
      detail: "Applied to the working source. Update review to capture it as an immutable revision.",
    };
  }
  return {
    label: "Ready to apply",
    tone: "border-violet-900/60 bg-violet-950/15 text-violet-300",
    detail: "Saved against this exact Review revision and source identity.",
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
  replacementText,
  intent,
  onReplacementChange,
  onIntentChange,
  onCreate,
  onCreateAndApply,
  onApply,
  onClose,
}: ReviewChangeProposalComposerProps) {
  const state = proposal ? proposalState(proposal) : null;
  const location = selection
    ? `${selection.path}:${selection.start_line}${selection.end_line !== selection.start_line ? `-${selection.end_line}` : ""}`
    : proposal
      ? `${proposal.path}:${proposal.start_line}${proposal.end_line !== proposal.start_line ? `-${proposal.end_line}` : ""}`
      : "";
  return (
    <section
      className={`review-composer review-source-composer ${directEdit ? "review-source-composer-edit" : "review-source-composer-proposal"}`}
      aria-label="Source change proposal"
      aria-busy={busy || loading}
    >
      <div className="review-composer-heading review-source-composer-heading">
        {directEdit
          ? <FilePenLine size={15} className="text-sky-300" aria-hidden="true" />
          : <Sparkles size={15} className="text-violet-300" aria-hidden="true" />}
        <span>{directEdit ? "Edit source" : "Suggested change"}</span>
        {location && <span className="review-source-location" title={location}>{location}</span>}
        {state && <span className={`review-source-state ${state.tone}`}>{state.label}</span>}
        {!directEdit && !proposal && (
          <span className="review-source-safety">Patch only · source unchanged</span>
        )}
        <span className="flex-1" />
        <button type="button" onClick={onClose} className="review-icon-button h-7 w-7" aria-label="Close source editor">
          <X size={14} aria-hidden="true" />
        </button>
      </div>

      {loading && !selection && (
        <div className="flex items-center gap-2 px-1 py-3 text-[11px] text-neutral-500">
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
              <textarea
                autoFocus
                aria-label="Replacement code"
                value={replacementText}
                onChange={(event) => onReplacementChange(event.currentTarget.value)}
                rows={Math.max(4, Math.min(12, replacementText.split("\n").length + 1))}
                spellCheck={false}
                className="review-field review-source-replacement"
              />
            </div>
          </div>
          <textarea
            aria-label="Proposal intent"
            value={intent}
            onChange={(event) => onIntentChange(event.currentTarget.value)}
            rows={2}
            placeholder={directEdit ? "Why are you changing this? Optional." : "Why is this suggestion better? Optional."}
            className="review-field review-source-intent"
          />
          <div className="review-composer-footer">
            <span className="review-composer-shortcut">Exact reviewed source · stale writes are blocked</span>
            <button
              type="button"
              disabled={busy || replacementText === selection.text}
              onClick={directEdit ? onCreateAndApply : onCreate}
              className="review-toolbar-button-primary"
            >
              {busy ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : directEdit ? <FilePenLine size={14} aria-hidden="true" /> : <Check size={14} aria-hidden="true" />}
              {busy ? (directEdit ? "Applying…" : "Saving…") : directEdit ? "Apply to source" : "Save suggestion"}
            </button>
          </div>
        </>
      )}

      {proposal && state && (
        <>
          <div className="review-source-preview-heading">
            <span>{directEdit ? "Patch preview" : "Suggested patch"}</span>
            <span>based on revision {proposal.base_revision_number}</span>
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
              <button type="button" disabled={busy} onClick={onApply} className="review-toolbar-button-primary">
                {busy ? <LoaderCircle size={14} className="animate-spin motion-reduce:animate-none" aria-hidden="true" /> : <FilePenLine size={14} aria-hidden="true" />}
                {busy ? "Applying…" : directEdit ? "Apply to source" : "Apply suggestion"}
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
