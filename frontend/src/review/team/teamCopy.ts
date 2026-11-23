import type { ReviewDirectoryRow, TeamState } from "../types";

/** The words of presence, claim and handoff (SPEC 4.16). Pure; the screens only place them. */

export function firstName(name: string): string {
  return name.trim().split(/\s+/)[0] || name;
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "";
  return (parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

/** "just now", "10 min ago", "2 h ago", "3 d ago": time only, as SPEC's "started 10 min ago". */
export function agoText(startedAt: string, now: number): string {
  const at = Date.parse(startedAt);
  if (!Number.isFinite(at)) return "a while ago";
  const minutes = Math.floor(Math.max(0, now - at) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
}

/** "Mon" inside the next week, else "Nov 16". */
export function untilText(until: string, now: number): string {
  const at = new Date(until);
  if (!Number.isFinite(at.getTime())) return "";
  const days = (at.getTime() - now) / 86_400_000;
  return days < 7
    ? at.toLocaleDateString("en-US", { weekday: "short" })
    : at.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** "Leo is away until Mon · reassigned to Maya", or, when the stand-in is not on the review, "… · their stand-in is not on this review". */
export function awayLine(name: string, until: string, reassignedTo: string | null, now: number): string {
  const when = untilText(until, now);
  if (!when) return "";
  const tail = reassignedTo ? `reassigned to ${firstName(reassignedTo)}` : "their stand-in is not on this review";
  return `${firstName(name)} is away until ${when} \u00b7 ${tail}`;
}

/** "Maya", "Maya and Leo", "Maya, Leo and Sam". */
export function nameList(names: readonly string[]): string {
  const first = names.map(firstName);
  return first.length <= 1 ? first.join("") : `${first.slice(0, -1).join(", ")} and ${first[first.length - 1]}`;
}

/** What my claim says about the people who were in the review: told only once their own screen got it. */
export function claimSuffix(claim: { told: readonly { name: string }[]; waiting: readonly { name: string }[] }): string {
  const told = claim.told.map((person) => person.name);
  const waiting = claim.waiting.map((person) => person.name);
  const parts = [
    told.length ? `${nameList(told)} ${told.length === 1 ? "was" : "were"} told` : "",
    waiting.length ? `${nameList(waiting)} will see it` : "",
  ].filter(Boolean);
  return parts.length ? ` \u00b7 ${parts.join(" \u00b7 ")}` : "";
}

export type TeamTone = "in" | "mine" | "other";

export interface TeamLine {
  tone: TeamTone;
  text: string;
  /** The avatar letters: the person the line is about. */
  who: string;
}

/**
 * The line on a Home row (SPEC 4.16): who is in it, who took it, who it was handed to. `null` where there is
 * nothing to say. A claim outranks a handoff made before it, and a handoff outranks presence, as the server decided.
 */
export function teamLine(team: ReviewDirectoryRow["team"], now: number): TeamLine | null {
  if (!team) return null;
  if (team.claim) {
    const claim = team.claim;
    if (claim.by_me) {
      return { tone: "mine", text: `Taken by you${claimSuffix(claim)}`, who: claim.by.name };
    }
    return { tone: "other", text: `Taken by ${firstName(claim.by.name)}`, who: claim.by.name };
  }
  if (team.handoff) {
    const h = team.handoff;
    const away = h.away ? awayLine(h.away.name, h.away.until, h.away.rerouted === false ? null : h.to.name, now) : "";
    const note = h.note ? ` · ${h.note}` : "";
    if (h.to_me) return { tone: "other", text: `Handed to you${h.by.name ? ` by ${h.by.name}` : ""}${note}`, who: h.by.name || h.to.name };
    return { tone: "other", text: `Handed off to ${h.to.name}${away ? ` · ${away}` : ""}${note}`, who: h.to.name };
  }
  const first = team.reviewing[0];
  if (!first) return null;
  return { tone: "in", text: `${firstName(first.name)} started ${agoText(first.started_at, now)} · from your team`, who: first.name };
}

/** True when opening this row should first ask (someone is in it and nobody took it or was given it). */
export function needsPresenceDialog(team: TeamState | undefined): boolean {
  return Boolean(team && team.reviewing.length > 0 && !team.claim && !team.handoff);
}

/** The dialog's body (SPEC 4.16): who started, what they have sent, and that notes stay private. */
export function presenceBody(person: { name: string; started_at: string; sent_notes?: number | null }, now: number): string {
  const sent = person.sent_notes;
  const middle = sent === undefined || sent === null
    ? ""
    : sent === 0 ? ` ${firstName(person.name)} has not sent anything yet.` : ` ${firstName(person.name)} has sent ${sent === 1 ? "1 note" : `${sent} notes`}.`;
  return `${person.name} started ${agoText(person.started_at, now)}.${middle} Your notes stay private until you send them.`;
}

/** The toast for a notice the server holds for me (someone took the review, or handed it to me). */
export function noticeText(notice: NonNullable<TeamState["notices"]>[number]): string {
  if (notice.kind === "taken") return `${firstName(notice.by.name)} took this review`;
  return `${notice.by.name || "A teammate"} handed this review to you${notice.note ? `: ${notice.note}` : ""}`;
}
