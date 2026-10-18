import { useEffect, useState } from "react";

import { Button as UiButton } from "../components/ui/button";
import { adoptRuleSuggestion, dismissRuleSuggestion } from "./reviewApi";
import type { ReviewOverview, ReviewRuleAdoptionProposal } from "./types";

export default function ReviewPolicySection({ overview }: { overview: ReviewOverview }) {
  const policy = overview.policy;
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  const [dismissError, setDismissError] = useState("");
  const [proposals, setProposals] = useState<Record<string, ReviewRuleAdoptionProposal>>({});
  const [proposalError, setProposalError] = useState("");

  useEffect(() => {
    setDismissed(new Set());
    setDismissError("");
    setProposals({});
    setProposalError("");
  }, [overview.revision.id]);

  if (!policy) return null;
  const suggestions = policy.suggestions.filter((item) => item.state === "suggested" && !dismissed.has(item.id));
  const visible =
    policy.future_changes.length > 0
    || suggestions.length > 0
    || policy.authority.errors.length > 0
    || policy.authority.rules.length > 0
    || policy.authority.ai_content_access.length > 0
    || policy.authority.base_state === "provisional";
  if (!visible) return null;

  return (
    <section className="mt-5" data-testid="review-policy-summary">
      <div className="review-kicker mb-2">Review policy</div>
      <div className="space-y-2 text-caption">
        <details className="rounded-md border border-border/80 px-2.5 py-2">
          <summary className="cursor-pointer text-neutral-300">
            Current authority · {policy.authority.rules.length} rule{policy.authority.rules.length === 1 ? "" : "s"}
            {policy.authority.ai_content_access.length > 0
              ? ` · ${policy.authority.ai_content_access.length} AI access scope${policy.authority.ai_content_access.length === 1 ? "" : "s"}`
              : ""}
          </summary>
          <div className="mt-2 space-y-1 text-muted-foreground">
            <div>Trusted base: {policy.authority.base_state}</div>
            {policy.authority.rules.map((rule) => (
              <div key={rule.ref} className="rounded border border-neutral-900 px-2 py-1.5">
                <div className="font-medium text-neutral-300">{rule.stable_key}</div>
                <div>{rule.instruction}</div>
                <div className="font-mono text-micro">
                  {rule.paths.length > 0 ? rule.paths.join(", ") : "all paths"} · {rule.origin}
                </div>
              </div>
            ))}
            {policy.authority.ai_content_access.map((access) => (
              <div key={access.digest} className="font-mono text-micro">
                {access.mode} · {access.paths.join(", ")} · {access.origin}
              </div>
            ))}
            {policy.authority.errors.map((error) => <div key={error} className="text-amber-300">{error}</div>)}
          </div>
        </details>

        {policy.future_changes.length > 0 && (
          <details className="rounded-md border border-amber-900/45 bg-amber-950/10 px-2.5 py-2" open>
            <summary className="cursor-pointer text-amber-200">
              Future policy · {policy.future_changes.length} change{policy.future_changes.length === 1 ? "" : "s"}
            </summary>
            <div className="mt-2 space-y-2">
              {policy.future_changes.map((change) => (
                <div key={`${change.category}:${change.path}`}>
                  <div className="font-mono text-neutral-300">{change.path}</div>
                  <div className="text-neutral-400">{change.summary}</div>
                  {change.effects.map((effect) => <div key={effect} className="text-muted-foreground">· {effect}</div>)}
                </div>
              ))}
            </div>
          </details>
        )}

        {suggestions.length > 0 && (
          <details className="rounded-md border border-sky-900/40 bg-sky-950/10 px-2.5 py-2">
            <summary className="cursor-pointer text-sky-200">Rule suggestions · {suggestions.length}</summary>
            <div className="mt-2 space-y-2">
              {suggestions.map((suggestion) => (
                <div key={suggestion.id} className="rounded border border-border/70 px-2 py-2">
                  <div className="flex items-start gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="text-neutral-300">{suggestion.candidate_statement}</div>
                      <div className="mt-0.5 font-mono text-micro text-muted-foreground">
                        {suggestion.paths.length > 0 ? suggestion.paths.join(", ") : "all paths"} · {suggestion.supporting_count} human comments
                      </div>
                      <div className="mt-0.5 text-micro text-sky-300/80">Suggestion only · not active</div>
                    </div>
                    <UiButton
                      variant="ghost"
                      size="compact"
                      type="button"
                      onClick={() => {
                        setProposalError("");
                        void adoptRuleSuggestion(overview.session.id, suggestion.id, overview.revision.id)
                          .then((result) => setProposals((current) => ({ ...current, [suggestion.id]: result.proposal })))
                          .catch((error: unknown) => setProposalError(error instanceof Error ? error.message : String(error)));
                      }}
                    >
                      Propose as rule
                    </UiButton>
                    <UiButton
                      variant="ghost"
                      size="compact"
                      type="button"
                      onClick={() => {
                        setDismissError("");
                        void dismissRuleSuggestion(overview.session.id, suggestion.id, overview.revision.id)
                          .then(() => setDismissed((current) => new Set(current).add(suggestion.id)))
                          .catch((error: unknown) => setDismissError(error instanceof Error ? error.message : String(error)));
                      }}
                    >
                      Dismiss
                    </UiButton>
                  </div>
                  {proposals[suggestion.id] && (
                    <div className="mt-2 space-y-1" data-testid="rule-adoption-proposal">
                      <div className="text-micro text-sky-300/80">
                        Proposed change to {proposals[suggestion.id].target_path} · not active until it lands on the trusted base
                      </div>
                      <pre className="max-h-48 overflow-auto rounded border border-border/70 px-2 py-1.5 font-mono text-micro text-neutral-300">
                        {proposals[suggestion.id].patch}
                      </pre>
                      <div className="text-micro text-muted-foreground">{proposals[suggestion.id].how_to_land}</div>
                      <UiButton
                        variant="ghost"
                        size="compact"
                        type="button"
                        onClick={() => { void navigator.clipboard?.writeText(proposals[suggestion.id].patch); }}
                      >
                        Copy patch
                      </UiButton>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </details>
        )}
        {proposalError && <div className="text-rose-300" role="alert">Could not propose rule: {proposalError}</div>}
        {dismissError && <div className="text-rose-300" role="alert">Could not dismiss suggestion: {dismissError}</div>}
      </div>
    </section>
  );
}
