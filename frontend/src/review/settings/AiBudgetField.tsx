import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import {
  BUDGET_KEY,
  BUDGET_LABEL,
  AI_PREFERENCES_PATH,
  budgetSavedToast,
  budgetStatusLine,
  budgetUseLine,
  parseAiBudget,
  parseBudgetInput,
  setAiBudget,
  useAiBudget,
} from "../aiBudget";
import { useFlowToast } from "../flowToast";
import { requestBrowserJson } from "../reviewApi";

export const BUDGET_INPUT_ERROR = "Enter 0 or more, or leave it empty for no limit.";

/**
 * The AI budget (SPEC 7.1 OWNER-3): a dollar amount per month, empty for no limit, with Save, a toast, a status line and
 * what this month has used. Its one home is Review flow › AI help; AI & background links here instead (review-23.3).
 */
export function AiBudgetField() {
  const id = useId();
  const budget = useAiBudget();
  const toast = useFlowToast();
  const [text, setText] = useState("");
  const [touched, setTouched] = useState(false);
  // A number field reports typed junk as an empty value; that must never read as "no limit".
  const [badInput, setBadInput] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const seeded = useRef(false);

  // Show what is saved until the person types; a later answer (a Save, a focus) refreshes it only while untouched.
  useEffect(() => {
    if (!budget || (touched && seeded.current)) return;
    setText(budget.monthly_budget_usd === null ? "" : String(budget.monthly_budget_usd));
    seeded.current = true;
  }, [budget, touched]);

  const parsed = badInput ? undefined : parseBudgetInput(text);
  const invalid = parsed === undefined;

  function save(event: FormEvent) {
    event.preventDefault();
    if (parsed === undefined) { setError(BUDGET_INPUT_ERROR); toast.show({ text: BUDGET_INPUT_ERROR, tone: "alert" }); return; }
    setError("");
    setSaving(true);
    requestBrowserJson<unknown>(`${AI_PREFERENCES_PATH}/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: { [BUDGET_KEY]: parsed } }),
    })
      .then((result) => {
        const next = parseAiBudget(result);
        if (next) setAiBudget(next);
        setTouched(false);
        setBadInput(false);
        toast.show({ text: budgetSavedToast(parsed) });
      })
      .catch((reason: unknown) => {
        const message = reason instanceof Error && reason.message ? reason.message : "Couldn't save the AI budget.";
        setError(message);
        toast.show({ text: message, tone: "alert" });
      })
      .finally(() => setSaving(false));
  }

  return (
    <>
    <form aria-label="AI budget" onSubmit={save} className="space-y-1" data-testid="ai-budget" noValidate>
      <label htmlFor={id} className="block text-label font-semibold pointer-coarse:flex pointer-coarse:items-center">{BUDGET_LABEL}</label>
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={id}
          type="number"
          inputMode="decimal"
          min={0}
          step="any"
          placeholder="No limit"
          value={text}
          disabled={!budget}
          aria-invalid={invalid || Boolean(error) || undefined}
          aria-describedby={error || (invalid && touched) ? `${id}-status ${id}-error` : `${id}-status`}
          className="w-40"
          onChange={(event) => { setText(event.target.value); setBadInput(event.target.validity?.badInput === true); setTouched(true); setError(""); }}
        />
        <Button type="submit" size="sm" disabled={!budget || saving}>{saving ? "Saving…" : "Save"}</Button>
      </div>
      {invalid && touched ? <p id={`${id}-error`} className="text-meta text-p1" data-testid="settings-ai-budget-invalid">{BUDGET_INPUT_ERROR}</p> : null}
      <p id={`${id}-status`} className="text-label" data-testid="settings-ai-budget-status">
        {budget ? budgetStatusLine(budget.monthly_budget_usd) : "Checking…"}
      </p>
      {budget ? <p className="text-meta text-muted-foreground" data-testid="settings-ai-budget-use">{budgetUseLine(budget)}</p> : null}
      {error && !(invalid && touched) ? <p id={`${id}-error`} className="text-label text-p1" data-testid="settings-ai-budget-error">{error}</p> : null}
    </form>
    {toast.element}
    </>
  );
}
