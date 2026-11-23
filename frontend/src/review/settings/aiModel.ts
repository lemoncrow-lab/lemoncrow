/**
 * What the AI help section may say about where code goes (SPEC 4.11, 7.1 P5-4 F). `intelligence.run_on` says where
 * the background job runs; the model comes from `intelligence.driver`. Only the driver can say whether code leaves
 * the machine, so every sentence about that is derived from it.
 */

export const RUN_ON_VALUES = ["auto", "local", "worker", "managed"] as const;
export type RunOn = (typeof RUN_ON_VALUES)[number];

const RUN_ON_LABEL: Readonly<Record<RunOn, string>> = { auto: "Automatic", local: "This machine", worker: "A worker", managed: "Managed by LemonCrow" };

/** The server accepts only these two from this page (`update_local_intelligence_settings`). */
const EDITABLE: readonly RunOn[] = ["auto", "local"];

export function runOnOptions(): { value: RunOn; label: string; disabled: boolean }[] {
  return RUN_ON_VALUES.map((value) => ({ value, label: RUN_ON_LABEL[value], disabled: !EDITABLE.includes(value) }));
}

export function readRunOn(raw: unknown): RunOn {
  return RUN_ON_VALUES.find((value) => value === raw) ?? "auto";
}

/** The read-only model line. Never claims the code stays on the machine unless the driver is a local model server. */
export function modelLine(driver: unknown): string {
  switch (driver) {
    case "ollama":
      return "Model: ollama, a local model server";
    case "auto":
    case undefined:
    case null:
    case "":
      return "Model: chosen automatically from the provider set up on this machine. Code in AI checks can go to that provider.";
    default:
      return `Model: ${String(driver)}, a provider. Code in AI checks can be sent to it.`;
  }
}
