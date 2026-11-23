/**
 * The words of the second reviewer (SPEC 4.16), pure. Every sentence is SPEC's; a name is the server's, and a count is
 * the server's count of signatures that exist and verify, never one made here.
 */

import type { SecondReviewerStatus } from "./secondApi";

export type StripTone = "neutral" | "warning" | "success" | "danger";

export const HOW_THIS_WORKS =
  "Always-a-decision areas need two people, chosen from CODEOWNERS for the area (never the author or you). Any \"Needs a change\" from either person blocks approval. The notes of both are sent. The record shows both signatures.";

export function firstName(name: string): string {
  return name.trim().split(/\s+/)[0] || name;
}

export function initials(name: string): string {
  const letters = name
    .trim()
    .split(/\s+/)
    .map((part) => part[0] ?? "")
    .join("");
  return letters.slice(0, 2).toUpperCase() || "?";
}

/** "15:04 UTC" from an ISO instant; empty when it is not one. */
export function utcClock(instant: string): string {
  const at = new Date(instant);
  if (Number.isNaN(at.getTime())) return "";
  return `${String(at.getUTCHours()).padStart(2, "0")}:${String(at.getUTCMinutes()).padStart(2, "0")} UTC`;
}

/** "Signed" is said only of a signature the server says exists and verified; anything else is still waited for. */
function givenState(status: SecondReviewerStatus): SecondReviewerStatus["state"] {
  const given = status.state === "agrees" || status.state === "disagrees";
  return given && status.second?.signed !== true ? "waiting" : status.state;
}

export interface Strip {
  /** The line beside the avatar. */
  line: string;
  chip: { text: string; tone: StripTone } | null;
}

/**
 * The strip on Overview and Wrap up. `choice` is the verdict picked on Wrap up (null on the Overview): a Request
 * changes pick asks nobody (D1). `null` when the server says no second signature is involved.
 */
export function stripFor(status: SecondReviewerStatus, choice: string | null): Strip | null {
  const who = status.reviewer;
  if (status.role === "second" || !who) return null;
  const state = givenState(status);
  const first = firstName(who.name);
  const area = status.area ? `CODEOWNER, ${status.area}` : "CODEOWNER";
  switch (state) {
    case "will_be_asked":
      if (choice === "changes_requested" || choice === "comment") return { line: "Not needed yet · asked when you approve", chip: null };
      return { line: `Second reviewer · ${who.name} (${area}) · asked when you approve`, chip: { text: "Will be asked", tone: "neutral" } };
    case "waiting":
      return { line: `Waiting for ${first}`, chip: { text: "Waiting", tone: "warning" } };
    case "agrees":
      return { line: `${first} signed · agrees`, chip: { text: "Agrees", tone: "success" } };
    case "disagrees": {
      const parts = status.second?.needs_change ?? [];
      const what = parts.length > 0 ? `${parts.join(", ")} needs a change` : "a part needs a change";
      return { line: `${first} disagrees: ${what}`, chip: { text: "Disagrees", tone: "danger" } };
    }
    case "not_needed_yet":
      return { line: "Not needed yet · asked when you approve", chip: null };
    default:
      return null;
  }
}

export interface DoneCopy {
  title: string;
  /** The bar's centre text. */
  bar: string;
  card: string;
  body: string;
  /**
   * Who is told, said as it is true (SPEC 7.1 P9-4 C): on a hosted server the author is told by their notifications, so
   * the first reviewer reads "You'll be told", never "Claude Code is told" (that needs an agent channel, which only a
   * local install has, and a second signature exists only on a hosted one). Empty once she has signed.
   */
  told: string;
  tone: "warning" | "success" | "danger";
}

/** Done for the first reviewer once a second signature is in play. `null` for any other state. */
export function doneFor(status: SecondReviewerStatus, revisionLabel: string): DoneCopy | null {
  const second = status.reviewer;
  const first = status.first_reviewer;
  if (!second || !first || status.role !== "first") return null;
  const { have, needed } = status.signatures ?? { have: 0, needed: 0 };
  const area = status.area ? `CODEOWNER, ${status.area}` : "CODEOWNER";
  const firstClock = utcClock(status.first?.signed_at ?? "");
  const secondClock = utcClock(status.second?.signed_at ?? "");
  const state = givenState(status);
  const notes = status.second?.note_sent ?? null;
  if (state === "waiting") {
    const signed = status.first?.signed && firstClock ? `${first.name} signed at ${firstClock}. ` : "";
    return {
      title: "Approval waiting for a second signature.",
      bar: "Waiting for 2nd signature",
      card: `Approval waiting for ${have} of ${needed} signatures · ${second.name}`,
      body: `${signed}${second.name} (${area}) signs next. Nothing counts as approved until both have signed. Nothing was merged.`,
      told: `You'll be told when ${firstName(second.name)} signs.`,
      tone: "warning",
    };
  }
  if (state === "agrees") {
    return {
      title: `Approved · ${have} of ${needed} signatures · ${first.name} and ${second.name}`,
      bar: `Approved · ${revisionLabel}`,
      card: `Approved · ${have} of ${needed} signatures`,
      body: `${second.name} signed${secondClock ? ` at ${secondClock}` : ""} and agrees. Both signatures are in the record. Nothing was merged.`,
      told: "",
      tone: "success",
    };
  }
  if (state === "disagrees") {
    return {
      title: `${firstName(second.name)} disagrees · the verdict is now Request changes`,
      bar: `Request changes · ${revisionLabel}`,
      card: `${second.name} signed${secondClock ? ` at ${secondClock}` : ""} and needs a change.`,
      body: notes
        ? `${firstName(second.name)}'s ${notes.count} ${notes.count === 1 ? "note" : "notes"} sent to ${notes.to}, with your notes. Nothing was merged.`
        : `${firstName(second.name)}'s note is saved on this review, with yours. Nothing was merged.`,
      told: "",
      tone: "danger",
    };
  }
  return null;
}

// ---------------------------------------------------------------- the second reviewer's own screen

export const SECOND_ANSWER_FIRST = "Answer on your own first.";
export const SECOND_COMPARE = (first: string) => `Compare with ${first}, then sign.`;
export const SECOND_HIDDEN_LEDE = (first: string, possessive: "His" | "Her" | "Their" = "Their") =>
  `${first} reviewed this change. ${possessive} answers stay hidden until you answer, so they don't steer yours.`;

export function signedLine(agrees: boolean, first: string): string {
  return agrees
    ? `Signed · you agree · ${first} sees it on the record.`
    : `Signed · you said a part needs a change · the verdict becomes Request changes.`;
}

/**
 * The tail of the Verify result's green line (SPEC 4.18): " · second signature @mchen · agrees · 14:20 UTC". Only a
 * second signature that exists and verified is named, and only on a Valid result; otherwise nothing is added.
 */
export function verifyTail(
  outcome: string,
  second: { state: string; reviewer: { handle: string; name: string } | null; signed_at: string } | undefined,
): string {
  if (outcome !== "valid" || !second || !second.reviewer) return "";
  const who = second.reviewer.handle ? `@${second.reviewer.handle}` : second.reviewer.name;
  const clock = utcClock(second.signed_at);
  if (second.state === "agrees") return ` \u00b7 second signature ${who} \u00b7 agrees${clock ? ` \u00b7 ${clock}` : ""}`;
  if (second.state === "disagrees") return ` \u00b7 second signature ${who} \u00b7 disagrees \u00b7 verdict changed to Request changes`;
  return "";
}
