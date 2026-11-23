import { useState } from "react";

import { Button as UiButton } from "../../components/ui/button";
import { Input as UiInput } from "../../components/ui/input";
import { Select, SelectItem } from "../../components/ui/select";
import { Textarea as UiTextarea } from "../../components/ui/textarea";
import { ToggleGroup } from "../../components/ui/toggle-group";
import { partName } from "../overview/overviewModel";
import type { ReviewDecisionEventInput, ReviewProjectionDecision, ReviewTarget } from "../types";

function targetLabel(target: ReviewTarget | undefined, fallback: string): string {
  return target ? `${partName(target)} · ${target.path}` : fallback;
}

function TargetLinks({
  ids,
  byId,
  onOpenTarget,
}: {
  ids: readonly string[];
  byId: ReadonlyMap<string, ReviewTarget>;
  onOpenTarget: (targetId: string) => void;
}) {
  if (ids.length === 0) return null;
  return (
    <ul className="flex flex-col gap-0.5">
      {ids.map((id) => (
        <li key={id}>
          <UiButton
            unstyled
            type="button"
            className="break-all text-left font-mono text-meta text-p2 hover:underline"
            onClick={() => onOpenTarget(id)}
          >
            {targetLabel(byId.get(id), id)}
          </UiButton>
        </li>
      ))}
    </ul>
  );
}


function optionLabel(decision: ReviewProjectionDecision, key: string): string {
  return decision.options.find((option) => option.key === key)?.label ?? key;
}

function triggerText(trigger: { kind: string; check_key: string; note: string } | null): string {
  if (!trigger) return "Left for later";
  if (trigger.kind === "new_snapshot") return "Left for later until the next revision";
  if (trigger.kind === "check_available") return `Left for later until ${trigger.check_key} has a result`;
  return trigger.note ? `Left for later until: ${trigger.note}` : "Left for later until you reopen it";
}

function statusLine(decision: ReviewProjectionDecision): { text: string; tone: string } | null {
  const state = decision.state;
  if (!state) return null;
  if (state.withdrawn) return { text: "Withdrawn by the author", tone: "text-muted-foreground" };
  if (state.disposition === "open") {
    if (decision.conflict) return { text: "Authors disagree", tone: "text-p1" };
    return { text: "Open", tone: "text-p1" };
  }
  if (state.disposition === "change_requested") {
    return state.navigable && state.reason
      ? { text: "Requested change resolved; decide again", tone: "text-p1" }
      : { text: "Change requested", tone: "text-p0" };
  }
  if (state.disposition === "deferred") {
    const base = triggerText(state.trigger);
    const text = state.latest_reason ? `${base} · ${state.latest_reason}` : base;
    return { text: state.trigger_fired ? `${text} · due now` : text, tone: "text-muted-foreground" };
  }
  const accepted = `Accepted · ${optionLabel(decision, state.accepted_option)}`;
  if (state.validity === "implementation_changed_since_acceptance") {
    return { text: "Code changed since acceptance", tone: "text-p1" };
  }
  if (state.validity === "needs_reconsideration") return { text: "Needs reconsideration", tone: "text-p0" };
  if (state.validity === "unverified") return { text: `${accepted} · unverified`, tone: "text-p1" };
  return { text: accepted, tone: "text-ok" };
}

type DecisionForm = "" | "request_change" | "defer";

/** One human decision: only the actions the server lists, each one write bound to this Snapshot by the Reader. */
export function DecisionCard({
  decision,
  byId,
  onOpenTarget,
  onAsk,
  onDecision,
}: {
  decision: ReviewProjectionDecision;
  byId: ReadonlyMap<string, ReviewTarget>;
  onOpenTarget: (targetId: string) => void;
  onAsk?: (targetId: string | null) => void;
  onDecision?: (key: string, input: ReviewDecisionEventInput) => Promise<void>;
}) {
  const actions = new Set(decision.state && onDecision ? decision.actions ?? [] : []);
  const actionable = [...actions].some((action) => action !== "ask");
  const [chosen, setChosen] = useState(decision.state?.accepted_option || decision.selected_option || "");
  const [form, setForm] = useState<DecisionForm>("");
  const [text, setText] = useState("");
  const [trigger, setTrigger] = useState("manual");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const status = statusLine(decision);

  const run = async (input: ReviewDecisionEventInput) => {
    if (!onDecision) return;
    setBusy(true);
    setFailure("");
    try {
      await onDecision(decision.key, input);
      setForm("");
      setText("");
    } catch (reason) {
      setFailure(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };
  const toggle = (next: DecisionForm) => {
    setText("");
    setForm((current) => (current === next ? "" : next));
  };

  return (
    <article data-testid={`brief-decision-${decision.key}`} className="rounded-md border border-border/80 px-2.5 py-2">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1 text-label font-medium text-foreground">{decision.question || decision.key}</div>
        {!decision.state && (
          <span className="shrink-0 rounded border border-border/70 px-1 text-micro uppercase tracking-wide text-muted-foreground">
            Proposed by the author · read-only
          </span>
        )}
      </div>
      {status && <div className={`text-caption ${status.tone}`}>{status.text}</div>}
      {decision.state && decision.state.changed_inputs.length > 0 && (
        <ul className="mt-0.5 list-disc pl-4 text-caption text-muted-foreground">
          {decision.state.changed_inputs.map((line) => <li key={line}>{line}</li>)}
        </ul>
      )}
      {decision.state?.limitations.map((line) => (
        <div key={line} className="text-caption text-muted-foreground">{line}</div>
      ))}
      {decision.stale && <div className="text-caption text-p1">Written for earlier content</div>}
      {decision.conflict && decision.positions && decision.positions.length > 0 && (
        <ul className="mt-1 flex flex-col gap-0.5 text-caption text-subtle">
          {decision.positions.map((position) => (
            <li key={position.producer}>{`${position.producer} chose ${optionLabel(decision, position.selected_option)}`}</li>
          ))}
        </ul>
      )}
      {actions.has("accept") ? (
        <ToggleGroup
          aria-label="Options"
          className="mt-1 flex-col items-start gap-1"
          itemClassName="h-auto min-h-7 py-0.5 text-left"
          disabled={busy}
          value={chosen}
          onChange={setChosen}
          options={decision.options.map((option) => ({
            value: option.key,
            label: (
              <span className="flex items-center gap-1.5">
                <span>{option.label}</span>
                {option.key === decision.selected_option && <span className="text-p2">author's choice</span>}
              </span>
            ),
          }))}
        />
      ) : (
        <ul className="mt-1 flex flex-col gap-0.5 text-caption">
          {decision.options.map((option) => (
            <li key={option.key} className={option.key === decision.selected_option ? "text-foreground" : "text-muted-foreground"}>
              <span>{option.label}</span>
              {option.key === decision.selected_option && <span className="ml-2 text-p2">author's choice</span>}
            </li>
          ))}
        </ul>
      )}
      {decision.reason_human_required && (
        <div className="mt-1 text-caption">
          <span className="text-muted-foreground">Why a human decides: </span>
          <span className="text-subtle">{decision.reason_human_required}</span>
        </div>
      )}
      <div className="mt-1.5">
        <TargetLinks ids={decision.target_ids} byId={byId} onOpenTarget={onOpenTarget} />
      </div>
      {actionable && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {actions.has("reconfirm") && (
            <UiButton variant="outline" size="compact" type="button" disabled={busy} onClick={() => void run({ action: "reconfirm" })}>
              Reconfirm
            </UiButton>
          )}
          {actions.has("accept") && (
            <UiButton
              variant="outline"
              size="compact"
              type="button"
              disabled={busy || !chosen}
              onClick={() => void run({ action: "accept", option_key: chosen })}
            >
              Accept decision
            </UiButton>
          )}
          {actions.has("request_change") && (
            <UiButton variant="outline" size="compact" type="button" disabled={busy} aria-expanded={form === "request_change"} onClick={() => toggle("request_change")}>
              Request change
            </UiButton>
          )}
          {actions.has("ask") && onAsk && (
            <UiButton variant="ghost" size="compact" type="button" onClick={() => onAsk(decision.target_ids[0] ?? null)}>Ask</UiButton>
          )}
          {actions.has("defer") && (
            <UiButton variant="ghost" size="compact" type="button" disabled={busy} aria-expanded={form === "defer"} onClick={() => toggle("defer")}>
              Defer
            </UiButton>
          )}
          {actions.has("reopen") && (
            <UiButton variant="ghost" size="compact" type="button" disabled={busy} onClick={() => void run({ action: "reopen" })}>
              Reopen
            </UiButton>
          )}
        </div>
      )}
      {form === "request_change" && (
        <div className="mt-1.5 flex flex-col gap-1">
          <UiTextarea uiSize="xs" aria-label="Requested change" rows={3} value={text} onChange={(event) => setText(event.target.value)} />
          <UiButton variant="outline" size="compact" type="button" className="self-start" disabled={busy || !text.trim()} onClick={() => void run({ action: "request_change", feedback_body: text.trim() })}>
            Send request
          </UiButton>
        </div>
      )}
      {form === "defer" && (
        <div className="mt-1.5 flex flex-col gap-1">
          <UiInput uiSize="xs" aria-label="Reason for deferring" value={text} onChange={(event) => setText(event.target.value)} />
          <Select uiSize="xs" aria-label="Revisit when" value={trigger} onValueChange={setTrigger}>
            <SelectItem value="manual">When I reopen it</SelectItem>
            <SelectItem value="new_snapshot">At the next revision</SelectItem>
            {decision.relied_checks.map((key) => (
              <SelectItem key={key} value={`check_available:${key}`}>{`When ${key} has a result`}</SelectItem>
            ))}
          </Select>
          <UiButton variant="outline" size="compact" type="button" className="self-start" disabled={busy || !text.trim()} onClick={() => void run({ action: "defer", reason: text.trim(), trigger })}>
            Defer decision
          </UiButton>
        </div>
      )}
      {failure && <div role="alert" className="mt-1 text-caption text-p0">{failure}</div>}
    </article>
  );
}
