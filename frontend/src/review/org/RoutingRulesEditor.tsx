import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { Checkbox } from "../../components/ui/checkbox";
import { Input } from "../../components/ui/input";
import { Select, SelectItem } from "../../components/ui/select";
import {
  CRITICALITIES, MAX_RULES, documentOf, draftOf, draftValid, emptyRule, ruleProblems,
  type Criticality, type RoutingDraft, type RuleRow,
} from "./routingRules";

const ANY = "any";

/**
 * Reviewer routing as rows (hosted-31.3): one rule per row, added and removed here, each row saying what is wrong with
 * it before Save. Saves the same JSON document the server stores (`routingRules.ts`).
 */
export function RoutingRulesEditor({ document, busy, onSave }: {
  document: Record<string, unknown> | null;
  busy: boolean;
  onSave: (document: Record<string, unknown> | null) => void;
}) {
  const [draft, setDraft] = useState<RoutingDraft>(() => draftOf(document));
  const [dirty, setDirty] = useState(false);
  // A saved (or reloaded) document replaces the rows; adjusted during render so the rows are not remounted on mount.
  const [source, setSource] = useState(document);
  if (source !== document) {
    setSource(document);
    setDraft(draftOf(document));
    setDirty(false);
  }

  const change = (next: RoutingDraft) => { setDraft(next); setDirty(true); };
  const update = (key: number, patch: Partial<RuleRow>) =>
    change({ ...draft, rules: draft.rules.map((rule) => (rule.key === key ? { ...rule, ...patch } : rule)) });
  const valid = draftValid(draft);

  return (
    <form className="space-y-3" aria-label="Reviewer routing rules" onSubmit={(event) => { event.preventDefault(); if (valid) onSave(documentOf(draft)); }}>
      <label className="flex items-center gap-2 text-label">
        <Checkbox checked={draft.automatic} onChange={(event) => change({ ...draft, automatic: event.target.checked, automaticNamed: true })} />
        Ask a rule's reviewers automatically (only rules that also say so)
      </label>
      {draft.rules.length === 0 ? <p className="text-label text-muted-foreground">No routing rules yet.</p> : null}
      <ol className="space-y-3" aria-label="Routing rules">
        {draft.rules.map((rule, index) => {
          const n = index + 1;
          const problems = dirty ? ruleProblems(rule, draft.rules) : [];
          return (
            <li key={rule.key} className="space-y-2 rounded-md border border-border p-3" data-testid="org-routing-rule">
              <div className="grid gap-2 sm:grid-cols-2">
                <label className="space-y-1 text-meta text-muted-foreground">
                  <span>Name</span>
                  <Input aria-label={`Name of rule ${n}`} value={rule.id} onChange={(event) => update(rule.key, { id: event.target.value })} aria-invalid={problems.some((p) => p.startsWith("Name"))} />
                </label>
                <label className="space-y-1 text-meta text-muted-foreground">
                  <span>Paths (comma-separated)</span>
                  <Input aria-label={`Paths of rule ${n}`} placeholder="src/payments/**, api/*.proto" value={rule.paths} onChange={(event) => update(rule.key, { paths: event.target.value })} aria-invalid={problems.some((p) => p.startsWith("Paths"))} />
                </label>
                <label className="space-y-1 text-meta text-muted-foreground">
                  <span>People</span>
                  <Input aria-label={`People of rule ${n}`} placeholder="maya, leo@acme.example" value={rule.users} onChange={(event) => update(rule.key, { users: event.target.value })} />
                </label>
                <label className="space-y-1 text-meta text-muted-foreground">
                  <span>Teams (org/team)</span>
                  <Input aria-label={`Teams of rule ${n}`} placeholder="acme/payments" value={rule.teams} onChange={(event) => update(rule.key, { teams: event.target.value })} aria-invalid={problems.some((p) => p.startsWith("Teams"))} />
                </label>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Select
                  uiSize="xs"
                  aria-label={`Criticality of rule ${n}`}
                  value={rule.minCriticality || ANY}
                  onValueChange={(next) => update(rule.key, { minCriticality: next === ANY ? "" : (next as Criticality) })}
                >
                  <SelectItem value={ANY}>Any criticality</SelectItem>
                  {CRITICALITIES.map((level) => <SelectItem key={level} value={level}>{`${level} or higher`}</SelectItem>)}
                </Select>
                <label className="flex items-center gap-2 text-label">
                  <Checkbox aria-label={`Ask rule ${n}'s reviewers automatically`} checked={rule.automatic} onChange={(event) => update(rule.key, { automatic: event.target.checked })} />
                  Ask automatically
                </label>
                <Button type="button" size="xs" variant="ghost" className="ml-auto" aria-label={`Remove rule ${n}`} onClick={() => change({ ...draft, rules: draft.rules.filter((other) => other.key !== rule.key) })}>
                  <Trash2 size={14} aria-hidden="true" />
                  Remove
                </Button>
              </div>
              {problems.length > 0 ? (
                <ul className="space-y-0.5 text-meta text-p0" data-testid="org-routing-problems">
                  {problems.map((problem) => <li key={problem}>{problem}</li>)}
                </ul>
              ) : null}
            </li>
          );
        })}
      </ol>
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" variant="outline" disabled={busy || draft.rules.length >= MAX_RULES} onClick={() => change({ ...draft, rules: [...draft.rules, emptyRule(draft.rules)] })}>
          <Plus size={14} aria-hidden="true" />
          Add rule
        </Button>
        <Button type="submit" size="sm" variant="outline" aria-label="Save reviewer routing" disabled={busy || !dirty || !valid}>Save</Button>
      </div>
    </form>
  );
}
