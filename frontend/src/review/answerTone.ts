/** How an answer button shows that it is the part's recorded answer: Looks good turns green, Needs a change amber. */
export type AnswerKind = "good" | "change" | "later";

const STATE_OF: Record<AnswerKind, string> = { good: "reviewed", change: "needs_changes", later: "later" };
const TONE_OF: Record<AnswerKind, string> = {
  good: "border-ok-line bg-ok-bg text-ok hover:bg-ok-bg",
  change: "border-p1-line bg-p1-bg text-p1 hover:bg-p1-bg",
  later: "border-foreground bg-accent hover:bg-accent",
};

/**
 * A phone answer dock button (One at a time, All files): the full name ("Needs a change", never "Change") wraps to two
 * lines inside its quarter of a 390 px dock instead of being shortened or cut off (review-6.5).
 */
export const PHONE_DOCK_BUTTON = "h-12 min-w-0 whitespace-normal px-1 text-center text-meta leading-tight";

/** {@link answerPressed} with the button's own classes kept in front. */
export function pressedIn(base: string, state: string | undefined, kind: AnswerKind): { className: string; "aria-pressed": boolean } {
  const pressed = answerPressed(state, kind);
  return { className: `${base} ${pressed.className}`.trim(), "aria-pressed": pressed["aria-pressed"] };
}

export function answerPressed(state: string | undefined, kind: AnswerKind): { className: string; "aria-pressed": boolean } {
  const on = state === STATE_OF[kind];
  return { className: on ? TONE_OF[kind] : "", "aria-pressed": on };
}
