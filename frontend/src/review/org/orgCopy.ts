/**
 * The organization page's words (SPEC 4.19). The organization's name ("Acme") comes from the deployment's own
 * configuration (SPEC 7.1 P9-5 A); a deployment without one says "organization" and "your organization".
 */
import { fmtWhen } from "../../lib/format";
import { resetLabel, usd } from "../aiBudget";
import type { OrgAiCap, OrgAuditRecord, OrgReviewSettings } from "./orgApi";

export const ORG_TITLE = "Organization settings";
export function orgTitle(name: string): string { return name ? `${name} organization settings` : ORG_TITLE; }
export function readOnlyLede(name: string): string {
  return `Read-only. Only ${name ? `${name} organization` : "organization"} admins can change these. They sit above repo policy and your own settings.`;
}
export const ADMIN_LEDE = "You are viewing as an organization admin. Changes apply to everyone in the organization and are written to the audit log.";
export function precedenceLine(name: string): string {
  return `${name || "Organization"} locks > repo policy > your settings > defaults. You can make privacy stricter, never looser.`;
}
export function aiLocked(name: string): string { return `Set by ${name || "your organization"} · locked`; }
export const AUDIT_NOTE = "An excerpt of recent organization changes.";
export const AUDIT_ACTORS_ADMINS_ONLY = "Read-only. Who made each change is shown to organization admins.";
export const NO_AUDIT = "No organization changes are recorded yet.";

export const CAP_LABEL = "Cost cap ($ per month)";
/** What a member holds beyond the direct organization role their row sets: through a group or a repository grant. */
export function heldElsewhere(direct: string | null, held: readonly string[]): string {
  const others = held.filter((role) => role !== direct);
  if (others.length === 0) return "";
  return `${direct ? "Also holds" : "Holds"} ${others.join(", ")} through a group or a repository grant`;
}
export const ROUTING_NOTE = "Suggests who should review which paths. Rules ask someone automatically only when both the document and the rule set automatic to true. Your operator's policy wins over these rules.";
export function capLine(cap: OrgAiCap): string {
  const limit = cap.monthly_budget_usd;
  if (limit === null) return "No cap.";
  if (limit === 0) return "$0 · no new AI hints. Reviews and AI results you already have still work.";
  return `AI hints stop at ${usd(limit)}/month. Reviews still work.`;
}
export function capUseLine(cap: OrgAiCap): string {
  const base = `${usd(cap.used_usd, { cents: true })} used this month`;
  return cap.unreported_calls > 0 ? `${base} · ${cap.unreported_calls} ${cap.unreported_calls === 1 ? "call" : "calls"} with cost not reported` : base;
}
export function capSavedToast(limit: number | null): string {
  return limit === null ? "Cost cap removed · no cap" : `Cost cap saved · ${usd(limit)}/month`;
}
/** The pause, in the organization's words: SPEC 4.19's "AI hints paused for Acme until Dec 1 · reviews still work". */
export function capPausedLine(name: string, resetsOn: string, zero: boolean): string {
  const who = name || "your organization";
  return zero ? `AI hints paused for ${who} · reviews still work` : `AI hints paused for ${who} until ${resetLabel(resetsOn)} · reviews still work`;
}
export const ROLES_NOTE = "Each answer is what this server enforces. A second reviewer and whoever may approve a change to the policy come from the repository's CODEOWNERS, not from an organization role.";
export function rolesOmitted(omitted: readonly string[]): string {
  return `${omitted.join(" and ")} ${omitted.length === 1 ? "is" : "are"} not ${omitted.length === 1 ? "a role" : "roles"} on this server.`;
}

const RUN_ON: Readonly<Record<string, string>> = { auto: "Automatic", local: "This machine", worker: "A worker", managed: "Managed by LemonCrow" };

export function whereAiRuns(settings: OrgReviewSettings["where_ai_runs"]): string {
  if (settings.ai_blocked) return "AI is turned off for this organization.";
  return `AI jobs run: ${RUN_ON[settings.run_on] ?? settings.run_on}.`;
}

export function retentionLine(retention: OrgReviewSettings["retention"]): string {
  return `Audit log ${retention.audit_days} days · archived reviews ${retention.archived_review_days} days · background jobs ${retention.job_days} days.`;
}

export function identityLines(identity: OrgReviewSettings["identity"]): string[] {
  return [
    identity.sso ? `Single sign-on${identity.sso_host ? ` through ${identity.sso_host}` : ""}` : "Single sign-on is not set up",
    identity.scim ? "SCIM provisioning is on" : "SCIM provisioning is off",
  ];
}

export function minimumLine(minimum: OrgReviewSettings["second_reviewer"]): string {
  const n = minimum.minimum_reviewers;
  // "At least 2 reviewers", not "2 reviewers at least"; zero is no minimum, not "at least 0".
  const line = n > 0 ? `At least ${n} ${n === 1 ? "reviewer" : "reviewers"}` : "No minimum number of reviewers";
  return `${line}${minimum.origin === "built_in_default" ? " · the built-in default" : ""}`;
}

const EVENT: Readonly<Record<string, string>> = {
  "organization.member.role": "Changed a member's role",
  "organization.member.suspend": "Suspended a member",
  "organization.member.reactivate": "Reactivated a member",
  "organization.member.remove": "Removed a member",
  "organization.group.member_add": "Added a member to a group",
  "organization.group.member_remove": "Removed a member from a group",
  "organization.repository.grant": "Granted repository access",
  "organization.repository.revoke": "Revoked repository access",
  "review.signing_key.create": "Created a signing key",
  "review.signing_key.rotate": "Rotated the signing key",
  "review.signing_key.revoke": "Revoked a signing key",
  "review.signing_key.refuse": "A signing key change was refused",
  "organization.ai_cap.update": "Changed the cost cap",
  "auth.allow": "Signed in",
  "auth.refuse": "Sign-in refused",
  "auth.break_glass": "Signed in with a break-glass token",
  "authz.allow": "Access allowed",
  "authz.refuse": "Access refused",
  "identity.establish": "Started a session",
  "identity.refresh": "Refreshed a session",
  "identity.revoke": "Ended a session",
  "identity.provision": "Provisioned a member",
  "identity.deprovision": "Deprovisioned a member",
  "identity.jwks_refresh": "Refreshed the sign-in keys",
};

/** An audit event in plain words; an event this page has no wording for is shown with its dots and underscores as spaces. */
export function auditEventLabel(event: string): string {
  const known = EVENT[event];
  if (known) return known;
  const words = event.replace(/[._]+/g, " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : event;
}

export function auditLine(record: OrgAuditRecord): string {
  const what = auditEventLabel(record.event);
  return record.decision === "refuse" ? `${what} (refused)` : what;
}

export function auditTime(record: OrgAuditRecord): string {
  return fmtWhen(record.ts);
}
