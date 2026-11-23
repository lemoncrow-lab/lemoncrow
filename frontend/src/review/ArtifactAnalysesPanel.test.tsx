import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ArtifactAnalysesPanel from "./ArtifactAnalysesPanel";
import type { AnalysisEntryRow, AnalysisRecord, ArtifactAnalysesProjection } from "./artifactAnalysesApi";

const api = vi.hoisted(() => ({
  fetchArtifactAnalyses: vi.fn(),
  postArtifactAnalysisRun: vi.fn(),
  fetchAnalysisArtifact: vi.fn(),
}));

vi.mock("./artifactAnalysesApi", async (importOriginal) => ({ ...(await importOriginal<typeof import("./artifactAnalysesApi")>()), ...api }));

const SPEC = {
  entry_id: "migrations.execute",
  id: "db-migrations",
  tool: "sql",
  root: "db/migrations",
  title: "SQL migrations · db/migrations",
  origin: "discovered" as const,
  source: "db/migrations/0001_init.up.sql",
  executable: true,
  reason: "",
  setup_hint: "",
  digest: "analysis:sha256:" + "a".repeat(64),
};

function entry(options: Partial<AnalysisEntryRow> = {}): AnalysisEntryRow {
  return {
    id: "migrations.execute",
    title: "Migration execution",
    config_key: "migrations",
    status: "run",
    reason: "",
    sides: "head",
    required: false,
    setup_hint: "",
    configured: false,
    specs: [SPEC],
    ...options,
  };
}

function record(options: Partial<AnalysisRecord> = {}): AnalysisRecord {
  return {
    entry_id: "migrations.execute",
    entry_title: "Migration execution",
    spec: SPEC,
    sides: ["new"],
    status: "PASS",
    reasons: ["applied 1 new migration(s); rollback restored the base schema", "1 destructive and 1 lock-heavy operation(s) flagged for review"],
    setup_hint: "",
    findings: [
      {
        rule: "drop_column",
        category: "destructive",
        subject: "db-migrations::drop_column::users.legacy",
        title: "Drops a column · users.legacy",
        summary: "Drops column users.legacy",
        severity: "high",
        critical: true,
        change: "changed",
        paths: ["db/migrations/0002.up.sql"],
      },
    ],
    summary: {},
    attempts: [
      {
        side: "new",
        state: "ok",
        reason: "",
        exit_code: 0,
        evidence_id: "evd-head",
        evidence_ref: "check:test:x",
        duration_ms: 100,
        reused: false,
        environment: "envfp:1",
        executor_class: "managed_sandbox",
      },
    ],
    artifacts: [{ name: "schema.diff", ref: "run-artifact:r:" + "b".repeat(64), digest: "b".repeat(64), bytes: 120, expires_at: null }],
    executor_class: "managed_sandbox",
    reproduce: { command: "lc review --reproduce <evidence-id|record.json>", evidence_ids: ["evd-head"] },
    evidence_id: "evd-record",
    observed_at: "2026-10-04T00:00:00Z",
    ...options,
  };
}

function projection(options: Partial<ArtifactAnalysesProjection> = {}): ArtifactAnalysesProjection {
  return {
    revision_id: "revision-2",
    historical: false,
    run_plan_digest: "digest",
    base_state: "present",
    errors: [],
    entries: [entry()],
    results: [],
    verdicts: [],
    ...options,
  };
}

describe("ArtifactAnalysesPanel", () => {
  beforeEach(() => {
    api.fetchArtifactAnalyses.mockReset();
    api.postArtifactAnalysisRun.mockReset();
    api.fetchAnalysisArtifact.mockReset();
  });

  it("renders nothing when the trusted base gives the analyses nothing to act on", async () => {
    api.fetchArtifactAnalyses.mockResolvedValue(projection({ entries: [] }));
    const { container } = render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-2" />);
    await waitFor(() => expect(api.fetchArtifactAnalyses).toHaveBeenCalledWith("review-1", "revision-2"));
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText("Artifact analyses")).not.toBeInTheDocument();
  });

  it("shows results with critical findings, a heavy artifact and the local reproduce command", async () => {
    api.fetchArtifactAnalyses.mockResolvedValue(projection({ results: [record()] }));
    api.fetchAnalysisArtifact.mockResolvedValue({ name: "schema.diff", expired: false, text: "+ tenant_id INTEGER", truncated: false, bytes: 20 });
    render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-2" />);

    const card = await screen.findByTestId("analysis-result-migrations.execute-db-migrations");
    expect(card).toHaveTextContent("Passed");
    expect(card).toHaveTextContent("managed sandbox");
    expect(within(card).getByTestId("analysis-finding")).toHaveTextContent("critical");
    expect(within(card).getByTestId("analysis-finding")).toHaveTextContent("Drops a column · users.legacy");
    expect(within(card).getByTestId("analysis-reproduce")).toHaveTextContent("lc review --reproduce evd-head");
    await userEvent.click(within(card).getByRole("button", { name: "schema.diff" }));
    expect(api.fetchAnalysisArtifact).toHaveBeenCalledWith("review-1", "revision-2", "evd-record", "schema.diff");
    expect(await within(card).findByText("+ tenant_id INTEGER")).toBeInTheDocument();
  });

  it("says when a raw artifact expired and when a run was limited by budget", async () => {
    api.fetchArtifactAnalyses.mockResolvedValue(
      projection({
        entries: [entry({ status: "limited_by_budget" })],
        results: [record({ status: "NOT_RUN", reasons: ["Limited by budget: not every side of this analysis ran"], findings: [] })],
      }),
    );
    api.fetchAnalysisArtifact.mockResolvedValue({ name: "schema.diff", expired: true, text: "", truncated: false, bytes: 0 });
    render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-2" />);

    const row = await screen.findByTestId("analysis-entry-migrations.execute");
    expect(row).toHaveTextContent("Limited by budget");
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
    const card = screen.getByTestId("analysis-result-migrations.execute-db-migrations");
    expect(card).toHaveTextContent("Not run");
    expect(card).toHaveTextContent("Limited by budget: not every side of this analysis ran");
    await userEvent.click(within(card).getByRole("button", { name: "schema.diff" }));
    expect(await within(card).findByText(/raw artifact expired/)).toBeInTheDocument();
  });

  it("shows a setup hint instead of a pass when a toolchain is missing", async () => {
    api.fetchArtifactAnalyses.mockResolvedValue(
      projection({
        entries: [entry({ id: "infra.render", title: "Infrastructure render diff", status: "not_available", setup_hint: "Install terraform" })],
        results: [record({ entry_id: "infra.render", status: "NOT_RUN", reasons: ["terraform is not installed in the sandbox"], setup_hint: "Install terraform in the executor image", findings: [], artifacts: [] })],
      }),
    );
    render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-2" />);

    expect(await screen.findByTestId("analysis-setup-hint")).toHaveTextContent("Install terraform in the executor image");
    expect(screen.getByTestId("analysis-entry-infra.render")).toHaveTextContent("Install terraform");
    expect(screen.queryByText("Passed")).not.toBeInTheDocument();
  });

  it("runs a planned entry and adds an unplanned one as a per-Review pick", async () => {
    api.fetchArtifactAnalyses.mockResolvedValue(
      projection({ entries: [entry(), entry({ id: "infra.render", title: "Infrastructure render diff", status: "not_planned" })] }),
    );
    api.postArtifactAnalysisRun.mockResolvedValue({ entry_id: "migrations.execute", revision_id: "revision-2", verdict: "PASS", executions: 1, reused: 0, notes: [], executor_class: "author_machine", run_on: "local" });
    render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-2" />);

    await userEvent.click(within(await screen.findByTestId("analysis-entry-migrations.execute")).getByRole("button", { name: "Run" }));
    expect(api.postArtifactAnalysisRun).toHaveBeenCalledWith("review-1", "revision-2", "migrations.execute", false);
    expect(await screen.findByRole("status")).toHaveTextContent("migrations.execute: Passed (1 run, 0 reused)");
    await waitFor(() => expect(api.fetchArtifactAnalyses).toHaveBeenCalledTimes(2));

    await userEvent.click(within(screen.getByTestId("analysis-entry-infra.render")).getByRole("button", { name: "Add & run" }));
    expect(api.postArtifactAnalysisRun).toHaveBeenLastCalledWith("review-1", "revision-2", "infra.render", true);
  });

  it("shows mixed-version pairings as behavior parts with the deploy order they ask for (item 41)", async () => {
    const pairing = (id: string, label: string, direction: "forward" | "reverse", state: string, reason: string, order?: string) => ({
      pairing: id,
      label,
      direction,
      state,
      reason,
      checked: 1,
      incompatible: state === "incompatible" ? 1 : 0,
      ...(order ? { order } : {}),
    });
    api.fetchArtifactAnalyses.mockResolvedValue(
      projection({
        results: [
          record({
            entry_id: "compat.mixed_version",
            status: "FAIL",
            findings: [{ ...record().findings[0], rule: "old_code_missing_column", title: "Old code reads users.legacy, which the migration drops" }],
            summary: {
              kind: "persistence",
              pairings: [
                pairing("old_code_new_schema", "Old code vs migrated schema", "forward", "incompatible", "1 base statement fails on the migrated schema"),
                pairing("new_code_old_schema", "New code vs old schema", "reverse", "incompatible", "1 head statement fails", "run the migration before deploying the new code"),
                pairing("set_heads_new_schema", "Change-set members' heads vs migrated schema", "forward", "not_available", "this Change is in no change set"),
              ],
              deploy_order: ["run the migration before deploying the new code"],
            },
          }),
        ],
      }),
    );
    render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-2" />);

    const card = await screen.findByTestId("analysis-result-compat.mixed_version-db-migrations");
    const rows = within(card).getAllByTestId("analysis-pairing");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Old code vs migrated schemaincompatible",
      "New code vs old schemaincompatible",
      "Change-set members' heads vs migrated schemanot available",
    ]);
    expect(rows[2].getAttribute("title")).toBe("this Change is in no change set");
    expect(within(card).getByTestId("analysis-deploy-order")).toHaveTextContent("Deploy order: run the migration before deploying the new code");
    expect(within(card).getByTestId("analysis-finding")).toHaveTextContent("Old code reads users.legacy, which the migration drops");
  });

  it("shows the flag on/off matrix with rows past the bound as limited by budget (item 44)", async () => {
    api.fetchArtifactAnalyses.mockResolvedValue(
      projection({
        results: [
          record({
            entry_id: "flags.matrix",
            status: "FAIL",
            findings: [],
            summary: {
              flags: [],
              matrix: [
                { state: "new_checkout=on", flag: "new_checkout", value: "on", env: {}, status: "regressed", base: { exit: 0, tests: 3 }, head: { exit: 1, tests: 3 }, tests_changed: 1 },
                { state: "new_checkout=off", flag: "new_checkout", value: "off", env: {}, status: "same", tests_changed: 0 },
                { state: "beta=on", flag: "beta", value: "on", env: {}, status: "limited_by_budget", reason: "Limited by budget: the standard depth runs at most 1 flag(s)" },
              ],
              bound: { depth: "standard", max_flags: 1, touched: 2, limited_flags: 1 },
              untouched: "default",
            },
          }),
        ],
      }),
    );
    render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-2" />);

    const card = await screen.findByTestId("analysis-result-flags.matrix-db-migrations");
    const rows = within(card).getAllByTestId("analysis-flag-state");
    expect(rows.map((row) => row.textContent)).toEqual(["new_checkout on1 test(s) changedregressed", "new_checkout offsame", "beta onLimited by budget"]);
    expect(rows[2].getAttribute("title")).toContain("at most 1 flag");
    expect(within(card).getByTestId("analysis-flag-bound")).toHaveTextContent("Limited by budget: 1 of 2 touched flags expanded at standard depth");
    expect(within(card).queryByTestId("analysis-pairing")).not.toBeInTheDocument();
  });

  it("is read-only on a historical revision", async () => {
    api.fetchArtifactAnalyses.mockResolvedValue(projection({ historical: true }));
    render(<ArtifactAnalysesPanel reviewId="review-1" revisionId="revision-1" />);
    await screen.findByTestId("artifact-analyses-panel");
    expect(screen.queryByRole("button", { name: "Run" })).not.toBeInTheDocument();
  });
});
