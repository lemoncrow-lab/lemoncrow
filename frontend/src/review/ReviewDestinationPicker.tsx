import { Button as UiButton } from "../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { Check, ChevronDown, Send } from "lucide-react";
import { useState, type ReactNode, type Ref } from "react";

import { fetchReviewDestinationOptions, putReviewDestination } from "./reviewApi";
import { sendCommentsLabel } from "./reviewPrimaryAction";
import type {
  FeedbackDeliveryCapability,
  ReviewDestinationKind,
  ReviewDestinationOption,
  ReviewDestinationState,
} from "./types";
import { fmtWhen } from "../lib/format";

function message(reason: unknown): string {
  return String(reason instanceof Error ? reason.message : reason);
}

function isCurrent(option: ReviewDestinationOption, saved: ReviewDestinationState["destination"] | null): boolean {
  const kind: ReviewDestinationKind = saved?.kind ?? "auto";
  if (option.kind !== kind) return false;
  if (kind === "session") return option.host === saved?.host && option.session_id === saved?.session_id;
  if (kind === "new_session") return option.host === saved?.host;
  if (kind === "github_pr") return option.external_ref === saved?.external_ref;
  return true;
}

function sessionTimestamp(value: string | undefined): { date: Date; label: string } | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return {
    date,
    label: fmtWhen(date),
  };
}
/**
 * The picker's sections, in reading order: hand it to a person, start a new
 * agent, or pick a session (the exact-author default first).
 */
const SECTIONS: readonly { label: string; kinds: readonly ReviewDestinationKind[] }[] = [
  { label: "To a person", kinds: ["export", "github_pr"] },
  { label: "New agent", kinds: ["new_session"] },
  { label: "Sessions", kinds: ["auto", "session"] },
];

/**
 * Where this Review's fixes go. Opening it lists what the reviewer can pick;
 * an option LemonCrow cannot reach stays visible with its reason.
 */
export function ReviewDestinationPicker({
  reviewId,
  disabled = false,
  onChanged,
  children,
}: {
  reviewId: string;
  disabled?: boolean;
  onChanged(state: ReviewDestinationState): void;
  children: ReactNode;
}) {
  const [options, setOptions] = useState<ReviewDestinationOption[] | null>(null);
  const [saved, setSaved] = useState<ReviewDestinationState["destination"] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = async () => {
    setLoading(true);
    setError("");
    try {
      const next = await fetchReviewDestinationOptions(reviewId);
      setOptions(next.options);
      setSaved(next.destination);
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoading(false);
    }
  };

  const choose = async (option: ReviewDestinationOption) => {
    setError("");
    try {
      const next = await putReviewDestination(reviewId, {
        kind: option.kind,
        ...(option.host ? { host: option.host } : {}),
        ...(option.session_id ? { session_id: option.session_id } : {}),
        ...(option.external_ref ? { external_ref: option.external_ref } : {}),
      });
      setSaved(next.destination);
      onChanged(next);
    } catch (reason) {
      setError(message(reason));
    }
  };

  return (
    <DropdownMenu onOpenChange={(open) => { if (open) void load(); }}>
      <DropdownMenuTrigger asChild disabled={disabled}>{children}</DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-80 max-h-[calc(100dvh-80px)]">
        <DropdownMenuLabel>Send fixes to</DropdownMenuLabel>
        {loading && !options && (
          <div className="px-2 py-1.5 text-caption text-muted-foreground">Finding sessions…</div>
        )}
        {error && (
          <div role="status" className="px-2 py-1.5 text-caption text-amber-300">{error}</div>
        )}
        {SECTIONS.map((section) => ({
          ...section,
          items: (options ?? [])
            .filter((option) => section.kinds.includes(option.kind))
            .sort((a, b) => section.kinds.indexOf(a.kind) - section.kinds.indexOf(b.kind)),
        }))
          .filter((section) => section.items.length > 0)
          .map((section, index) => (
            <DropdownMenuGroup key={section.label} aria-label={section.label}>
              {index > 0 && <DropdownMenuSeparator />}
              <div className="px-2 pb-0.5 pt-1.5 text-caption uppercase tracking-wide text-muted-foreground">{section.label}</div>
              {section.items.map((option) => {
                const current = isCurrent(option, saved);
                const timestamp = option.kind === "session" ? sessionTimestamp(option.activity_at) : null;
                return (
                  <DropdownMenuItem
                    key={`${option.kind}:${option.host ?? ""}:${option.session_id ?? ""}:${option.external_ref ?? ""}`}
                    className="items-start"
                    disabled={!option.available || current}
                    onSelect={() => void choose(option)}
                  >
                    <span className="mt-0.5 w-3 shrink-0">{current && <Check size={14} aria-label="current" />}</span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-baseline justify-between gap-2">
                        <span className="block min-w-0 truncate" title={option.label}>{option.kind === "auto" ? `${option.label} · default` : option.label}</span>
                        {timestamp && (
                          <time
                            dateTime={timestamp.date.toISOString()}
                            title={timestamp.date.toLocaleString()}
                            className="shrink-0 whitespace-nowrap text-caption tabular-nums text-muted-foreground"
                          >
                            {timestamp.label}
                          </time>
                        )}
                      </span>
                      <span className="block text-caption text-muted-foreground">{option.available ? option.detail : option.reason}</span>
                    </span>
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuGroup>
          ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** The chip's words for a route: "Claude a1b2", "PR #12", "Copy for the author". */
export function destinationChipLabel(delivery: FeedbackDeliveryCapability | null): string {
  if (!delivery) return "author sessions";
  if (delivery.kind === "export") return "Copy for the author";
  return delivery.label || delivery.host || "coding agent";
}

/** The Send action with a separate destination dropdown; the header shows it only while comments are unsent. */
export function ReviewHandoffControls({
  reviewId,
  delivery,
  unsent,
  busy,
  onSendNow,
  onDestinationChanged,
  buttonRef,
  busyLabel,
}: {
  reviewId: string;
  delivery: FeedbackDeliveryCapability | null;
  unsent: number;
  busy: boolean;
  onSendNow(): void;
  onDestinationChanged(state: ReviewDestinationState): void;
  buttonRef?: Ref<HTMLButtonElement>;
  busyLabel?: string;
}) {
  const label = destinationChipLabel(delivery);
  const unavailable = Boolean(delivery && !delivery.supported);
  const reason = unavailable
    ? delivery?.reason || "This destination cannot receive feedback"
    : delivery?.fallback
      ? delivery.reason || "No exact author session"
      : undefined;
  const action = busyLabel ?? sendCommentsLabel(unsent);
  return (
    <div className="flex shrink-0 items-center">
      <UiButton
        ref={buttonRef}
        variant="brand"
        type="button"
        disabled={busy}
        onClick={onSendNow}
        aria-label={action}
        tooltip={reason ?? `Preview feedback for ${label} and choose a review outcome. Nothing is sent until you confirm.`}
        className="rounded-r-none px-3 review-reader-primary"
      >
        <Send size={14} aria-hidden="true" />
        <span>{action}</span>
      </UiButton>
      <ReviewDestinationPicker reviewId={reviewId} disabled={busy} onChanged={onDestinationChanged}>
        <UiButton
          variant="brand"
          type="button"
          aria-label="Change where fixes go"
          title={`Fixes go to: ${label}`}
          className="rounded-l-none border-l-0 px-1"
        >
          <ChevronDown size={14} aria-hidden="true" />
        </UiButton>
      </ReviewDestinationPicker>
    </div>
  );
}
