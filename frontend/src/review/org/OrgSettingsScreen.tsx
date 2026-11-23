import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { Button } from "../../components/ui/button";
import { isDialogOpen } from "../../components/ui/dialog";
import { useFlowToast } from "../flowToast";
import { isColdLoad } from "../reviewBootLocation";
import { Input } from "../../components/ui/input";
import { Select, SelectItem } from "../../components/ui/select";
import { PageFrame } from "../../components/WorkbenchUI";
import { SettingsRow } from "../settings/settingsShell";
import { RoutingRulesEditor } from "./RoutingRulesEditor";
import { parseBudgetInput } from "../aiBudget";
import {
  ADMIN_LEDE, aiLocked, AUDIT_ACTORS_ADMINS_ONLY, AUDIT_NOTE, auditLine, auditTime, CAP_LABEL, capLine, capPausedLine,
  capSavedToast, capUseLine, heldElsewhere, identityLines, minimumLine, NO_AUDIT, orgTitle, precedenceLine, readOnlyLede,
  retentionLine, ROLES_NOTE, rolesOmitted, ROUTING_NOTE, whereAiRuns,
} from "./orgCopy";
import {
  changeMemberRole, fetchOrgDirectory, fetchOrgRouting, fetchOrgSettings, revokeSigningKey, rotateSigningKey, saveAiCap, saveOrgRouting,
  type OrgDirectory, type OrgReviewSettings, type OrgRouting,
} from "./orgApi";
import { fmtWhen } from "../../lib/format";

type Loaded = { state: "loading" } | { state: "failed"; message: string } | { state: "ready"; settings: OrgReviewSettings; directory: OrgDirectory | null; routing: OrgRouting | null };


/**
 * `/settings/org` (SPEC 4.19, POL-2, POL-4). Every member can read it; its controls exist only for an organization
 * admin, and the server refuses them for anyone else (a hidden control is never the only gate). Hosted only: `absent`
 * is called when this server has no such page (a local install), so the route can send the person back to Settings.
 * Esc returns to `settingsPath` and puts focus on that page's H1 (SPEC 3.6). It sits inside the app's Settings, which
 * already navigates, so it has that page's header (eyebrow, then the H1) and no Back bar of its own (hosted-31.1).
 */
export function OrgSettingsScreen({ settingsPath, onAbsent }: { settingsPath: string; onAbsent: () => void }) {
  const navigate = useNavigate();
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const toast = useFlowToast();
  const [revoking, setRevoking] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [capText, setCapText] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const absent = useRef(onAbsent);
  absent.current = onAbsent;

  const load = useCallback(async () => {
    try {
      const settings = await fetchOrgSettings();
      if (settings === null) { absent.current(); return; }
      // Only an admin asks for the member list; the server would refuse anyone else.
      const directory = settings.viewer.admin ? await fetchOrgDirectory().catch(() => null) : null;
      const routing = settings.viewer.admin ? await fetchOrgRouting().catch(() => null) : null;
      setLoaded({ state: "ready", settings, directory, routing });
    } catch (reasonFailed) {
      setLoaded({ state: "failed", message: reasonFailed instanceof Error && reasonFailed.message ? reasonFailed.message : "Couldn't read the organization settings." });
    }
  }, []);
  useEffect(() => { void load(); }, [load]);
  // SPEC 5.3: focus never moves on the first load.
  useEffect(() => { if (loaded.state === "ready" && !isColdLoad()) heading.current?.focus({ preventScroll: true }); }, [loaded.state]);

  const goBack = useCallback(() => navigate(settingsPath, { state: { focusHeading: true } }), [navigate, settingsPath]);
  // Esc returns to Settings (SPEC 3.6) unless a field, dialog or menu has it.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || isDialogOpen()) return;
      const target = event.target as HTMLElement | null;
      if (target && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))) return;
      event.preventDefault();
      goBack();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [goBack]);

  const act = async (run: () => Promise<unknown>, done: string) => {
    setBusy(true);
    toast.dismiss();
    try {
      await run();
      setRevoking("");
      setReason("");
      await load();
      toast.show({ text: done });
    } catch (failure) {
      toast.show({ text: failure instanceof Error && failure.message ? failure.message : "That change was not made.", tone: "alert" });
    } finally {
      setBusy(false);
    }
  };

  const ready = loaded.state === "ready" ? loaded : null;
  const admin = ready?.settings.viewer.admin === true;
  const name = ready?.settings.organization.name ?? "";
  const cap = ready?.settings.ai_cap ?? null;
  const capField = capText ?? (cap?.monthly_budget_usd == null ? "" : String(cap.monthly_budget_usd));
  const capValue = parseBudgetInput(capField);
  const saveCap = () => {
    if (capValue === undefined) return;
    void act(async () => { await saveAiCap(capValue); setCapText(null); }, capSavedToast(capValue));
  };
  const routing = ready?.routing ?? null;
  const saveRouting = (document: Record<string, unknown> | null) =>
    void act(() => saveOrgRouting(document), document ? "Reviewer routing saved" : "Reviewer routing removed");

  return (
    <div data-testid="org-settings">
    <PageFrame className="max-w-5xl">
      <div className="py-5">
        <p className="font-mono text-micro uppercase tracking-widest text-muted-foreground">Organization</p>
        <h1 ref={heading} tabIndex={-1} className="mt-1 text-lead font-semibold text-foreground outline-none" data-testid="org-title">{ready ? orgTitle(name) : orgTitle("")}</h1>
        {ready ? (
          <p className="mt-1 text-label text-muted-foreground" data-testid="org-lede">{admin ? ADMIN_LEDE : readOnlyLede(name)}</p>
        ) : null}
      </div>
      <div className="space-y-4 border-t border-border py-6">
        {loaded.state === "loading" ? <p className="text-label">Reading the organization settings…</p> : null}
        {loaded.state === "failed" ? <p role="alert" className="text-label">{loaded.message}</p> : null}
        {ready ? (
          <>
            <p className="text-label" data-testid="org-precedence">{precedenceLine(name)}</p>

            <SettingsRow id="org-ai" title="Where AI runs">
              <p className="text-label" data-testid="org-ai-line">{whereAiRuns(ready.settings.where_ai_runs)}</p>
              {ready.settings.where_ai_runs.locked ? <p className="text-meta text-muted-foreground" data-testid="org-ai-locked">{aiLocked(name)}</p> : null}
            </SettingsRow>

            <SettingsRow id="org-retention" title="Data retention">
              <p className="text-label" data-testid="org-retention-line">{retentionLine(ready.settings.retention)}</p>
            </SettingsRow>

            {cap ? (
              <SettingsRow id="org-cap" title="Cost cap">
                <p className="text-label" data-testid="org-cap-line">{capLine(cap)}</p>
                <p className="text-meta text-muted-foreground" data-testid="org-cap-use">{capUseLine(cap)}</p>
                {!cap.may_run ? <p className="text-label font-semibold" data-testid="org-cap-paused">{capPausedLine(name, cap.resets_on, cap.reason_code === "org_cap_zero")}</p> : null}
                {admin ? (
                  <form className="flex flex-wrap items-center gap-2" onSubmit={(event) => { event.preventDefault(); saveCap(); }}>
                    <label className="text-label" htmlFor="org-cap-input">{CAP_LABEL}</label>
                    <Input id="org-cap-input" inputMode="decimal" value={capField} onChange={(event) => setCapText(event.target.value)} className="max-w-[10rem]" aria-invalid={capValue === undefined} />
                    <Button type="submit" size="sm" variant="outline" disabled={busy || capValue === undefined || capText === null}>Save</Button>
                  </form>
                ) : null}
              </SettingsRow>
            ) : null}

            <SettingsRow id="org-identity" title="Identity">
              {identityLines(ready.settings.identity).map((line) => <p key={line} className="text-label">{line}</p>)}
            </SettingsRow>

            <SettingsRow id="org-keys" title="Signing keys">
              {!ready.settings.signing.available ? <p className="text-label text-muted-foreground">{ready.settings.signing.reason || "Record signing is not set up."}</p> : null}
              {ready.settings.signing.keys.length === 0 && ready.settings.signing.available ? <p className="text-label text-muted-foreground">No signing key yet.</p> : null}
              <ul className="space-y-2" aria-label="Signing keys">
                {ready.settings.signing.keys.map((key) => (
                  <li key={key.key_id} className="space-y-1" data-testid="org-key" data-status={key.status}>
                    <p className="text-label"><code>{key.key_id}</code> · {key.status} · {key.algorithm}{key.revoked_at ? ` · revoked from ${fmtWhen(key.revoked_at)}: ${key.revocation_reason}` : ""}</p>
                    {admin && key.status !== "revoked" ? (
                      revoking === key.key_id ? (
                        <form className="flex flex-wrap items-center gap-2" onSubmit={(event) => { event.preventDefault(); if (reason.trim()) void act(() => revokeSigningKey(key.key_id, reason.trim()), "Key revoked"); }}>
                          <Input aria-label="Revocation reason" value={reason} onChange={(event) => setReason(event.target.value)} className="max-w-xs" />
                          <Button type="submit" size="sm" variant="outline" disabled={busy || !reason.trim()}>Revoke key</Button>
                          <Button type="button" size="sm" variant="ghost" onClick={() => setRevoking("")}>Cancel</Button>
                        </form>
                      ) : (
                        <Button size="sm" variant="outline" disabled={busy} onClick={() => { setRevoking(key.key_id); setReason(""); }}>Revoke…</Button>
                      )
                    ) : null}
                  </li>
                ))}
              </ul>
              {admin && ready.settings.signing.available ? (
                <Button size="sm" variant="outline" disabled={busy} onClick={() => void act(rotateSigningKey, "Signing key rotated")}>
                  {ready.settings.signing.active_key_id ? "Rotate" : "Create signing key"}
                </Button>
              ) : null}
            </SettingsRow>

            <SettingsRow id="org-reviewers" title="Minimum reviewers">
              <p className="text-label">{minimumLine(ready.settings.second_reviewer)}</p>
            </SettingsRow>

            {admin && routing ? (
              <SettingsRow id="org-routing" title="Reviewer routing">
                <p className="text-meta text-muted-foreground">{ROUTING_NOTE}</p>
                <RoutingRulesEditor document={routing.routing} busy={busy} onSave={saveRouting} />
              </SettingsRow>
            ) : null}

            <SettingsRow id="org-roles" title="Roles">
              {ready.settings.roles.length === 0 ? <p className="text-label text-muted-foreground">This organization's roles are kept by its identity provider.</p> : (
                <table className="w-full text-left text-label" data-testid="org-roles">
                  <thead><tr><th scope="col" className="font-semibold">Role</th><th scope="col" className="font-semibold">People</th></tr></thead>
                  <tbody>{ready.settings.roles.map((row) => <tr key={row.role}><td>{row.role}</td><td>{row.people}</td></tr>)}</tbody>
                </table>
              )}
              {ready.settings.roles_table.rows.length > 0 ? (
                <div className="space-y-1">
                  <div className="overflow-x-auto">
                    <table className="w-full min-w-[34rem] text-left text-label" data-testid="org-roles-table">
                      <thead>
                        <tr>
                          <th scope="col" className="font-semibold">Role</th>
                          {ready.settings.roles_table.columns.map((column) => <th key={column.key} scope="col" className="font-semibold">{column.label}</th>)}
                        </tr>
                      </thead>
                      <tbody>
                        {ready.settings.roles_table.rows.map((row) => (
                          <tr key={row.name} data-testid="org-roles-table-row">
                            <th scope="row" className="font-normal">{row.name}</th>
                            {ready.settings.roles_table.columns.map((column) => <td key={column.key}>{row.cells[column.key] ? "Yes" : "No"}</td>)}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="text-meta text-muted-foreground">{ROLES_NOTE}</p>
                  {ready.settings.roles_table.omitted.length > 0 ? <p className="text-meta text-muted-foreground">{rolesOmitted(ready.settings.roles_table.omitted)}</p> : null}
                </div>
              ) : null}
              {admin && ready.directory ? (
                <ul className="space-y-2" aria-label="Members" data-testid="org-members">
                  {ready.directory.users.map((member) => {
                    // hosted-31.4: the same held roles the Role and People counts above are made of.
                    const elsewhere = heldElsewhere(member.role, member.held_roles ?? []);
                    return (
                      <li key={member.id || member.subject} className="flex flex-wrap items-center gap-2 text-label">
                        <code>{member.subject}</code>
                        <Select
                          uiSize="xs"
                          aria-label={`Role for ${member.subject}`}
                          value={member.role ?? ""}
                          placeholder={elsewhere ? "No direct role" : "No organization role"}
                          disabled={busy || !ready.directory?.management.editable || !member.id}
                          onValueChange={(next) => { if (next && next !== member.role) void act(() => changeMemberRole(member.id, next), "Role changed"); }}
                        >
                          {ready.directory?.management.organization_roles.map((organizationRole) => <SelectItem key={organizationRole} value={organizationRole}>{organizationRole}</SelectItem>)}
                        </Select>
                        {elsewhere ? <span className="text-meta text-muted-foreground" data-testid="org-member-held">{elsewhere}</span> : null}
                      </li>
                    );
                  })}
                </ul>
              ) : null}
            </SettingsRow>

            <SettingsRow id="org-audit" title="Audit log">
              <p className="text-meta text-muted-foreground">{AUDIT_NOTE}</p>
              {!ready.settings.audit.actors_visible ? <p className="text-meta text-muted-foreground" data-testid="org-audit-readonly">{AUDIT_ACTORS_ADMINS_ONLY}</p> : null}
              {ready.settings.audit.records.length === 0 ? <p className="text-label">{NO_AUDIT}</p> : (
                <ul className="space-y-1" data-testid="org-audit">
                  {ready.settings.audit.records.map((record) => (
                    <li key={record.id} className="text-label" data-testid="org-audit-row">{auditTime(record)} · {auditLine(record)}{record.principal ? <> · <code>{record.principal}</code></> : null}</li>
                  ))}
                </ul>
              )}
            </SettingsRow>
          </>
        ) : null}
      </div>
    </PageFrame>
      {toast.element}
    </div>
  );
}
