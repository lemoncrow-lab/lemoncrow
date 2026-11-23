import type { ReviewAiSetupHelp } from "./types";

/** The code a failed Ask carries when no AI model is set up (stream error event or structured HTTP detail). */
export const ASK_NOT_SET_UP = "ai_not_set_up";

/** The structured setup help a failed Ask carried, if any. */
export function askSetupHelp(reason: unknown): ReviewAiSetupHelp | undefined {
  if (!(reason instanceof Error)) return undefined;
  const carried = reason as Error & { setup_help?: ReviewAiSetupHelp; detail?: { setup_help?: ReviewAiSetupHelp } };
  return carried.setup_help ?? carried.detail?.setup_help;
}

/** True when an Ask failed only because no AI model is set up. */
export function askNotSetUp(reason: unknown): boolean {
  if (!(reason instanceof Error)) return false;
  const carried = reason as Error & { code?: string; detail?: { code?: string } };
  return (carried.code ?? carried.detail?.code) === ASK_NOT_SET_UP;
}
