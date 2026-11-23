import { hostedJson } from "../hostedOnly";

/** `GET /v1/organization/review-settings` (hosted only; SPEC 4.19, 7.1 P0-2). Every member reads it. */
export interface OrgSigningKey {
  key_id: string;
  algorithm: string;
  provider: string;
  status: "active" | "retired" | "revoked" | string;
  not_before: string;
  not_after: string;
  revoked_at: string;
  revocation_reason: string;
}

export interface OrgAuditRecord {
  id: string;
  ts: number;
  event: string;
  decision: string;
  principal: string;
  reason_code: string;
}

/** The cap block, in the same shape as the personal AI budget (`aiBudget.ts`), plus the organization's name. */
export interface OrgAiCap {
  monthly_budget_usd: number | null;
  used_usd: number;
  unreported_calls: number;
  complete: boolean;
  resets_on: string;
  may_run: boolean;
  reason_code: string;
  reason: string;
  organization: string;
}

export interface OrgRolesTable {
  columns: { key: string; label: string }[];
  rows: { name: string; basis: string; cells: Record<string, boolean> }[];
  omitted: string[];
}

export interface OrgReviewSettings {
  org_id: string;
  viewer: { admin: boolean };
  where_ai_runs: { run_on: string; ai_blocked: boolean; locked: boolean };
  retention: { audit_days: number; archived_review_days: number; job_days: number };
  identity: { sso: boolean; sso_host: string; scim: boolean };
  signing: { available: boolean; reason: string; active_key_id?: string; rotation_due_at?: string; keys: OrgSigningKey[] };
  second_reviewer: { minimum_reviewers: number; origin: string };
  roles: { role: string; people: number }[];
  audit: { visible: boolean; actors_visible: boolean; retention_days: number; records: OrgAuditRecord[] };
  /** "" when this deployment has not been given one. */
  organization: { name: string };
  ai_cap: OrgAiCap | null;
  roles_table: OrgRolesTable;
}

/** `GET /v1/organization` (admins only): the members an admin may re-role. */
export interface OrgMember {
  id: string;
  subject: string;
  active: boolean;
  email_domain: string;
  role: string | null;
  /** Every role held, directly, on a repository or through a group: what the Role and People counts are made of. */
  held_roles?: string[];
  is_self: boolean;
}
export interface OrgDirectory {
  users: OrgMember[];
  management: { editable: boolean; reason: string; organization_roles: string[] };
}

export const fetchOrgSettings = () => hostedJson<OrgReviewSettings>("/v1/organization/review-settings");
export const fetchOrgDirectory = () => hostedJson<OrgDirectory>("/v1/organization");

/** Admin-only on the server (403 for anyone else); each writes an audit record. */
export const rotateSigningKey = () => hostedJson<unknown>("/v1/organization/review-signing/keys/rotate", { method: "POST", body: "{}" });
export const revokeSigningKey = (keyId: string, reason: string) =>
  hostedJson<unknown>(`/v1/organization/review-signing/keys/${encodeURIComponent(keyId)}/revoke`, { method: "POST", body: JSON.stringify({ reason }) });
export const changeMemberRole = (memberId: string, role: string) =>
  hostedJson<unknown>(`/v1/organization/members/${encodeURIComponent(memberId)}/role`, { method: "PUT", body: JSON.stringify({ role }) });

/** The organization's own reviewer routing: `{automatic?, rules?}`, the shape of the operator policy's `routing`. */
export interface OrgRouting {
  routing: Record<string, unknown> | null;
  updated_by: string;
  updated_at: string;
}
export const fetchOrgRouting = () => hostedJson<OrgRouting>("/v1/organization/review-routing");
/** Admin-only on the server; audited. A document with any error is refused whole; `null` removes it. */
export const saveOrgRouting = (routing: Record<string, unknown> | null) =>
  hostedJson<OrgRouting>("/v1/organization/review-routing", { method: "PUT", body: JSON.stringify({ routing }) });

/** Admin-only on the server (403 for anyone else); audited. `null` removes the cap. */
export const saveAiCap = (monthlyCapUsd: number | null) =>
  hostedJson<{ ai_budget: OrgAiCap }>("/v1/organization/ai-cap", { method: "PUT", body: JSON.stringify({ monthly_cap_usd: monthlyCapUsd }) });
