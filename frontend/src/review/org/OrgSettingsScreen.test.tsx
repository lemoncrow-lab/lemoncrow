import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fmtWhen } from "../../lib/format";
import * as api from "./orgApi";
import { OrgSettingsScreen } from "./OrgSettingsScreen";
import { auditLine, identityLines, minimumLine, retentionLine, whereAiRuns } from "./orgCopy";

vi.mock("./orgApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./orgApi")>()),
  fetchOrgSettings: vi.fn(),
  fetchOrgDirectory: vi.fn(),
  rotateSigningKey: vi.fn(),
  revokeSigningKey: vi.fn(),
  changeMemberRole: vi.fn(),
  saveAiCap: vi.fn(),
  fetchOrgRouting: vi.fn(),
  saveOrgRouting: vi.fn(),
}));
const settings = vi.mocked(api.fetchOrgSettings);
const directory = vi.mocked(api.fetchOrgDirectory);
const rotate = vi.mocked(api.rotateSigningKey);
const revoke = vi.mocked(api.revokeSigningKey);
const role = vi.mocked(api.changeMemberRole);
const saveCap = vi.mocked(api.saveAiCap);
const readRouting = vi.mocked(api.fetchOrgRouting);
const saveRouting = vi.mocked(api.saveOrgRouting);
const CAP: api.OrgAiCap = { monthly_budget_usd: 500, used_usd: 120.5, unreported_calls: 0, complete: true, resets_on: "2026-12-01", may_run: true, reason_code: "", reason: "", organization: "Acme" };
const ROLES_TABLE: api.OrgRolesTable = {
  columns: [{ key: "review", label: "Can review" }, { key: "second", label: "Can be second reviewer" }, { key: "policy", label: "Can edit repo policy" }, { key: "org", label: "Can change org settings" }, { key: "records", label: "Can read all records" }],
  rows: [
    { name: "Author", basis: "on their own change", cells: { review: false, second: false, policy: false, org: false, records: false } },
    { name: "Reviewer", basis: "organization role", cells: { review: true, second: false, policy: false, org: false, records: false } },
    { name: "CODEOWNER", basis: "from CODEOWNERS", cells: { review: true, second: true, policy: true, org: false, records: false } },
    { name: "Org admin", basis: "organization role", cells: { review: true, second: false, policy: false, org: true, records: true } },
  ],
  omitted: ["Repo admin", "Auditor (read-only)"],
};

const KEYS = [
  { key_id: "rk_2", algorithm: "ed25519", provider: "dev", status: "active", not_before: "2026-11-01T00:00:00Z", not_after: "", revoked_at: "", revocation_reason: "" },
  { key_id: "rk_1", algorithm: "ed25519", provider: "dev", status: "retired", not_before: "2026-10-01T00:00:00Z", not_after: "2026-11-01T00:00:00Z", revoked_at: "", revocation_reason: "" },
];
function payload(admin: boolean): api.OrgReviewSettings {
  return {
    org_id: "org",
    viewer: { admin },
    where_ai_runs: { run_on: "managed", ai_blocked: false, locked: true },
    retention: { audit_days: 365, archived_review_days: 90, job_days: 90 },
    identity: { sso: true, sso_host: "id.example.com", scim: true },
    signing: { available: true, reason: "", active_key_id: "rk_2", rotation_due_at: "", keys: KEYS },
    second_reviewer: { minimum_reviewers: 1, origin: "built_in_default" },
    roles: [{ role: "admin", people: 1 }, { role: "reviewer", people: 2 }],
    audit: { visible: true, actors_visible: admin, retention_days: 365, records: [{ id: "a1", ts: 1_700_000_000, event: "review.signing_key.rotate", decision: "allow", principal: admin ? "usr_leo" : "", reason_code: "" }] },
    organization: { name: "Acme" },
    ai_cap: CAP,
    roles_table: ROLES_TABLE,
  };
}
const DIRECTORY: api.OrgDirectory = {
  users: [{ id: "m1", subject: "usr_sam", active: true, email_domain: "", role: "reviewer", is_self: false }],
  management: { editable: true, reason: "", organization_roles: ["admin", "developer", "reviewer"] },
};

function Where() {
  const location = useLocation();
  return <p data-testid="where">{`${location.pathname}|${JSON.stringify(location.state)}`}</p>;
}
function show(onAbsent = vi.fn()) {
  render(
    <MemoryRouter initialEntries={["/settings/org"]}>
      <Routes>
        <Route path="/settings/org" element={<OrgSettingsScreen settingsPath="/settings/review-preferences" onAbsent={onAbsent} />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
  return onAbsent;
}

beforeEach(() => {
  settings.mockReset().mockResolvedValue(payload(false));
  directory.mockReset().mockResolvedValue(DIRECTORY);
  rotate.mockReset().mockResolvedValue({});
  revoke.mockReset().mockResolvedValue({});
  role.mockReset().mockResolvedValue({});
  saveCap.mockReset().mockResolvedValue({ ai_budget: CAP });
  readRouting.mockReset().mockResolvedValue({ routing: null, updated_by: "", updated_at: "" });
  saveRouting.mockReset().mockResolvedValue({ routing: null, updated_by: "", updated_at: "" });
});
afterEach(cleanup);

describe("reviewer routing on the organization page", () => {
  it("is hidden from a member, who never asks for it", async () => {
    show();
    await screen.findByTestId("org-lede");
    expect(screen.queryByRole("form", { name: "Reviewer routing rules" })).toBeNull();
    expect(readRouting).not.toHaveBeenCalled();
  });

  it("edits rules as rows, holds back an invalid row, and saves the same JSON (hosted-31.3)", async () => {
    settings.mockResolvedValue(payload(true));
    readRouting.mockResolvedValue({
      routing: { automatic: true, rules: [{ id: "payments", paths: ["src/payments/**"], teams: ["acme/payments"], min_criticality: "high" }] },
      updated_by: "", updated_at: "",
    });
    show();
    await screen.findByRole("form", { name: "Reviewer routing rules" });
    expect(screen.getByLabelText("Name of rule 1")).toHaveProperty("value", "payments");
    expect(screen.getByLabelText("Paths of rule 1")).toHaveProperty("value", "src/payments/**");
    const save = screen.getByRole("button", { name: "Save reviewer routing" });
    expect(save).toHaveProperty("disabled", true);

    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(screen.getAllByTestId("org-routing-rule")).toHaveLength(2);
    // A new row names no paths and no reviewers: it says so and Save waits.
    expect(within(screen.getAllByTestId("org-routing-rule")[1]).getByTestId("org-routing-problems")).toHaveTextContent("Paths: name at least one path");
    expect(save).toHaveProperty("disabled", true);
    await userEvent.type(screen.getByLabelText("Paths of rule 2"), "docs/**, README.md");
    await userEvent.type(screen.getByLabelText("Teams of rule 2"), "not a team");
    expect(screen.getByText("Teams: write each team as org/team.")).toBeTruthy();
    await userEvent.clear(screen.getByLabelText("Teams of rule 2"));
    await userEvent.type(screen.getByLabelText("People of rule 2"), "maya");
    await userEvent.click(screen.getByRole("checkbox", { name: "Ask rule 2's reviewers automatically" }));
    expect(save).toHaveProperty("disabled", false);
    await userEvent.click(save);
    await waitFor(() => expect(saveRouting).toHaveBeenLastCalledWith({
      automatic: true,
      rules: [
        { id: "payments", paths: ["src/payments/**"], teams: ["acme/payments"], min_criticality: "high" },
        { id: "rule-2", paths: ["docs/**", "README.md"], users: ["maya"], automatic: true },
      ],
    }));
  });

  it("removes the document when every rule is removed and nothing is set", async () => {
    settings.mockResolvedValue(payload(true));
    readRouting.mockResolvedValue({ routing: { rules: [{ id: "docs", paths: "docs/**", users: ["maya"] }] }, updated_by: "", updated_at: "" });
    show();
    await userEvent.click(await screen.findByRole("button", { name: "Remove rule 1" }));
    expect(screen.getByText("No routing rules yet.")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Save reviewer routing" }));
    await waitFor(() => expect(saveRouting).toHaveBeenCalledWith(null));
  });
});

describe("the page's header (hosted-31.1)", () => {
  it("is the Settings pages' header, with no Back bar of its own", async () => {
    show();
    await screen.findByTestId("org-lede");
    expect(screen.getByText("Organization", { selector: "p" })).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).className).toContain("text-lead");
    expect(screen.queryByRole("button", { name: "Back, go back" })).toBeNull();
  });
});

describe("each member's row and the role counts read the same roles (hosted-31.4)", () => {
  it("says what a member holds through a group when the row sets no direct role", async () => {
    settings.mockResolvedValue(payload(true));
    directory.mockResolvedValue({
      ...DIRECTORY,
      users: [
        { id: "m1", subject: "usr_sam", active: true, email_domain: "", role: null, held_roles: ["admin"], is_self: false },
        { id: "m2", subject: "usr_leo", active: true, email_domain: "", role: "reviewer", held_roles: ["developer", "reviewer"], is_self: false },
        { id: "m3", subject: "usr_kim", active: true, email_domain: "", role: null, held_roles: [], is_self: false },
      ],
    });
    show();
    const rows = await screen.findAllByTestId("org-member-held");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Holds admin through a group or a repository grant",
      "Also holds developer through a group or a repository grant",
    ]);
    expect(screen.getByRole("combobox", { name: "Role for usr_sam" })).toHaveTextContent("No direct role");
    expect(screen.getByRole("combobox", { name: "Role for usr_kim" })).toHaveTextContent("No organization role");
  });
});

describe("a member reads the organization page and cannot change it (POL-2)", () => {
  it("shows the precedence line, a locked 'Where AI runs', and the read-only lede", async () => {
    show();
    expect(await screen.findByRole("heading", { level: 1, name: "Acme organization settings" })).toBeTruthy();
    expect(screen.getByTestId("org-lede").textContent).toBe("Read-only. Only Acme organization admins can change these. They sit above repo policy and your own settings.");
    expect(screen.getByTestId("org-precedence").textContent).toBe("Acme locks > repo policy > your settings > defaults. You can make privacy stricter, never looser.");
    expect(screen.getByTestId("org-ai-locked").textContent).toBe("Set by Acme · locked");
    expect(screen.getByTestId("org-retention-line").textContent).toBe("Audit log 365 days · archived reviews 90 days · background jobs 90 days.");
  });

  it("says 'your organization' where this deployment has no name", async () => {
    settings.mockResolvedValue({ ...payload(false), organization: { name: "" } });
    show();
    expect(await screen.findByRole("heading", { level: 1, name: "Organization settings" })).toBeTruthy();
    expect(screen.getByTestId("org-ai-locked").textContent).toBe("Set by your organization · locked");
    expect(screen.getByTestId("org-lede").textContent).toContain("Only organization admins can change these");
  });

  it("reads the cost cap and the roles table, and changes neither", async () => {
    show();
    await screen.findByTestId("org-lede");
    expect(screen.getByTestId("org-cap-line").textContent).toBe("AI hints stop at $500/month. Reviews still work.");
    expect(screen.getByTestId("org-cap-use").textContent).toBe("$120.50 used this month");
    expect(screen.queryByTestId("org-cap-paused")).toBeNull();
    expect(screen.queryByRole("textbox", { name: "Cost cap ($ per month)" })).toBeNull();
    const rows = screen.getAllByTestId("org-roles-table-row").map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent).join(","));
    expect(rows[0]).toBe("No,No,No,No,No");
    expect(rows[2]).toBe("Yes,Yes,Yes,No,No");
    expect(screen.getByText("Repo admin and Auditor (read-only) are not roles on this server.")).toBeTruthy();
  });

  it("says the pause in the organization's words, with the date it ends", async () => {
    settings.mockResolvedValue({ ...payload(false), ai_cap: { ...CAP, used_usd: 500, may_run: false, reason_code: "org_cap_used_up" } });
    show();
    expect((await screen.findByTestId("org-cap-paused")).textContent).toBe("AI hints paused for Acme until Dec 1 · reviews still work");
  });

  it("has no control that changes anything, and the audit log is for admins", async () => {
    show();
    await screen.findByTestId("org-lede");
    for (const name of [/^Rotate/, /^Revoke/, /^Create signing key/]) expect(screen.queryByRole("button", { name })).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByTestId("org-members")).toBeNull();
    // The audit excerpt is read-only for a member: what happened and when, never who did it.
    expect(screen.getByTestId("org-audit-readonly").textContent).toBe("Read-only. Who made each change is shown to organization admins.");
    expect(screen.getByTestId("org-audit-row").textContent).toBe(`${fmtWhen(1_700_000_000)} · Rotated the signing key`);
    expect(directory).not.toHaveBeenCalled();
    expect(screen.getAllByTestId("org-key")).toHaveLength(2);
  });
});

describe("an organization admin edits it, and every edit goes through the server", () => {
  beforeEach(() => { settings.mockResolvedValue(payload(true)); });

  it("says so, lists the audit excerpt, and rotates the signing key", async () => {
    show();
    expect((await screen.findByTestId("org-lede")).textContent).toContain("You are viewing as an organization admin.");
    expect(screen.getByTestId("org-audit-row").textContent).toContain("Rotated the signing key");
    await userEvent.click(screen.getByRole("button", { name: "Rotate" }));
    expect(rotate).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByTestId("flow-toast").textContent).toContain("Signing key rotated"));
    expect(settings).toHaveBeenCalledTimes(2); // re-read after the change
  });

  it("sets the cost cap and removes it, with the server's own save", async () => {
    show();
    const field = await screen.findByRole("textbox", { name: "Cost cap ($ per month)" });
    expect((field as HTMLInputElement).value).toBe("500");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.clear(field);
    await userEvent.type(field, "250");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(saveCap).toHaveBeenLastCalledWith(250);
    await waitFor(() => expect(screen.getByTestId("flow-toast").textContent).toContain("Cost cap saved · $250/month"));
    await userEvent.clear(screen.getByRole("textbox", { name: "Cost cap ($ per month)" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(saveCap).toHaveBeenLastCalledWith(null);
  });

  it("refuses a cap that is not a number of dollars", async () => {
    show();
    const field = await screen.findByRole("textbox", { name: "Cost cap ($ per month)" });
    await userEvent.clear(field);
    await userEvent.type(field, "lots");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    expect(saveCap).not.toHaveBeenCalled();
  });

  it("revokes a key only with a reason", async () => {
    show();
    const key = (await screen.findAllByTestId("org-key"))[0];
    await userEvent.click(within(key).getByRole("button", { name: "Revoke…" }));
    expect((screen.getByRole("button", { name: "Revoke key" }) as HTMLButtonElement).disabled).toBe(true);
    await userEvent.type(screen.getByRole("textbox", { name: "Revocation reason" }), "leaked");
    await userEvent.click(screen.getByRole("button", { name: "Revoke key" }));
    expect(revoke).toHaveBeenCalledWith("rk_2", "leaked");
  });

  it("changes a member's role from the list", async () => {
    show();
    await userEvent.click(await screen.findByRole("combobox", { name: "Role for usr_sam" }));
    await userEvent.click(screen.getByRole("option", { name: "developer" }));
    expect(role).toHaveBeenCalledWith("m1", "developer");
  });

  it("shows the server's refusal instead of pretending the change was made", async () => {
    rotate.mockRejectedValue(new Error("principal is not authorized to administer the index"));
    show();
    await userEvent.click(await screen.findByRole("button", { name: "Rotate" }));
    await waitFor(() => expect(screen.getByTestId("flow-toast").textContent).toContain("principal is not authorized to administer the index"));
  });
});

describe("leaving the page (SPEC 3.6, POL-4)", () => {
  it("Esc returns to Settings and asks for focus on its H1", async () => {
    show();
    await screen.findByTestId("org-lede");
    await userEvent.keyboard("{Escape}");
    expect((await screen.findByTestId("where")).textContent).toBe('/settings/review-preferences|{"focusHeading":true}');
  });

  it("Esc inside a field belongs to the field", async () => {
    settings.mockResolvedValue(payload(true));
    show();
    const key = (await screen.findAllByTestId("org-key"))[0];
    await userEvent.click(within(key).getByRole("button", { name: "Revoke…" }));
    await userEvent.click(screen.getByRole("textbox", { name: "Revocation reason" }));
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTestId("where")).toBeNull();
  });

});

describe("a local install has no such page (SPEC 7.1 P0-2)", () => {
  it("reports itself absent and draws no organization control", async () => {
    settings.mockResolvedValue(null);
    const onAbsent = show();
    await waitFor(() => expect(onAbsent).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("org-lede")).toBeNull();
    expect(screen.queryByRole("button", { name: /^Rotate/ })).toBeNull();
  });
});

describe("the page's sentences", () => {
  it("say only what the server reports", () => {
    expect(whereAiRuns({ run_on: "managed", ai_blocked: false, locked: true })).toBe("AI jobs run: Managed by LemonCrow.");
    expect(whereAiRuns({ run_on: "managed", ai_blocked: true, locked: true })).toBe("AI is turned off for this organization.");
    expect(identityLines({ sso: false, sso_host: "", scim: false })).toEqual(["Single sign-on is not set up", "SCIM provisioning is off"]);
    expect(minimumLine({ minimum_reviewers: 2, origin: "organization" })).toBe("At least 2 reviewers");
    expect(minimumLine({ minimum_reviewers: 1, origin: "built_in_default" })).toBe("At least 1 reviewer · the built-in default");
    expect(minimumLine({ minimum_reviewers: 0, origin: "built_in_default" })).toBe("No minimum number of reviewers · the built-in default");
    expect(retentionLine({ audit_days: 1, archived_review_days: 2, job_days: 3 })).toContain("Audit log 1 days");
    expect(auditLine({ id: "", ts: 0, event: "organization.member.role", decision: "refuse", principal: "", reason_code: "" })).toBe("Changed a member's role (refused)");
    expect(auditLine({ id: "", ts: 0, event: "x.unknown", decision: "allow", principal: "", reason_code: "" })).toBe("X unknown");
  });
});
