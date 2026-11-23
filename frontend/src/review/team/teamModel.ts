import { hostedJson } from "../hostedOnly";

/** `GET /v1/reviews/team-health` (hosted only; team leads, who are the organization's admins; SPEC 4.19, 7.1 P7-3). */
export interface TeamRepo {
  repo_id: string;
  repo: string;
  open_reviews: number;
  waiting_reviews: number;
  median_wait_seconds: number | null;
  decisions_asked: number;
  decisions_signed: number;
  older_than_1_day: number;
}
export interface TeamHealth {
  scope: "repository";
  window: "now";
  repositories: TeamRepo[];
}

export const TEAM_TITLE = "Team health";
export const TEAM_LEDE = "How review is going in each repo right now. Per repo, never per person.";
export const TEAM_FOOTER = "Numbers here are per repository, never per person";
export const NOT_A_LEAD = "Only team leads can see this page.";
export const NO_REPOS = "No repository has an open review.";

export const fetchTeamHealth = () => hostedJson<TeamHealth>("/v1/reviews/team-health");

/** "3 h 20 min", "45 min", "2 d 4 h"; nothing waiting reads as a dash, never as zero minutes. */
export function waitText(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return "under a minute";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} d ${hours % 24} h` : `${days} d`;
}

export function signaturesText(repo: Pick<TeamRepo, "decisions_asked" | "decisions_signed">): string {
  return repo.decisions_asked === 0 ? "—" : `${repo.decisions_signed} of ${repo.decisions_asked}`;
}

/** The rows of one repo's card, in SPEC's order. */
export function repoRows(repo: TeamRepo): { label: string; value: string }[] {
  return [
    { label: "Median time waiting", value: waitText(repo.median_wait_seconds) },
    { label: "Changes that always need a decision, signed by two people", value: signaturesText(repo) },
    { label: "Reviews older than 1 day", value: String(repo.older_than_1_day) },
  ];
}
