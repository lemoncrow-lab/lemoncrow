/**
 * The refusal a local Review browser gets before the terminal has connected it.
 *
 * Kept apart from the fetch layer so the Reader can name the cause without importing network code.
 */

/** Shown when this browser has no usable local capability and no CLI pairing is armed. */
export const LOCAL_BROWSER_RECONNECT_MESSAGE =
  "This browser is not connected to LemonCrow Review. Run `lc review --open` in your terminal to reconnect.";

/**
 * Whether a failed load is the refusal above, not an account that lost access. The page opened from a printed link
 * (`lc review --no-open`) is the usual case: nothing armed a pairing for this browser.
 */
export function isLocalBrowserUnpaired(reason: unknown): boolean {
  const failure = reason as { status?: unknown; message?: unknown } | null;
  return failure?.status === 401 && failure.message === LOCAL_BROWSER_RECONNECT_MESSAGE;
}
