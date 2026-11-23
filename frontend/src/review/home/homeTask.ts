import { askedPlanned, visibleText } from "../overview/overviewModel";
import { hostLabel } from "../developmentLabels";
import type { ReviewJournal } from "../types";
import type { HeroTask } from "./HomeRows";

/** The task the agent was given, from its session journal (UNTRUSTED text); null when the session recorded none (the Asked block is then left out). */
export function askedFromJournal(journal: Pick<ReviewJournal, "entries" | "host" | "task">): HeroTask | null {
  // The user's first message when the session recorded one, else the task the session was started with.
  const asked = askedPlanned(journal.entries).asked || visibleText(journal.task ?? "");
  if (!asked) return null;
  return { text: asked, host: journal.host ? hostLabel(journal.host) : "" };
}
