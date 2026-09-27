import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ReviewOverviewSheet from "./ReviewOverviewSheet";
import {
  fetchReviewDependencies,
  fetchReviewEvidenceContent,
  fetchReviewVerification,
  waiveReviewVerificationCheck,
} from "./reviewApi";
import type { ReviewOverview, ReviewSurfaceInfo, ReviewVerificationCheck, ReviewVerificationProjection } from "./types";


vi.mock("./reviewApi", () => ({
  fetchReviewVerification: vi.fn(),
  fetchReviewDependencies: vi.fn(),
  fetchReviewEvidenceContent: vi.fn(),
  waiveReviewVerificationCheck: vi.fn(),
}));

const OVERVIEW = {
  title: "Review",
  brief: {
    summary: "12 files across Review history, Review readiness",
    themes: ["Review history", "Review readiness"],
    major_changes: [
      {
        key: "intent:review-history",
        label: "Review history",
        explanation: "Grounded in commit: Durable Change identity. Key targets: historyProjection.",
        file_count: 6,
        target_count: 3,
        attention_count: 1,
        reviewed: 1,
        changed_since_review: 1,
        needs_changes: 0,
        pending: 2,
        first_path: "frontend/src/review/ReviewHistorySheet.tsx",
      },
      {
        key: "intent:review-readiness",
        label: "Review readiness",
        explanation: "Grounded in commit: Durable Change identity. Key targets: readinessProjection.",
        file_count: 6,
        target_count: 2,
        attention_count: 0,
        reviewed: 2,
        changed_since_review: 0,
        needs_changes: 0,
        pending: 0,
        first_path: "frontend/src/review/ReviewReadinessSheet.tsx",
      },
    ],
    review_first: [],
    verification: { pass: 1, fail: 0, not_run: 0, unknown: 0 },
    annotations: { human: 0, author: 0, lemoncrow: 0, ai_review: 0 },
    artifacts: { current: 1, stale: 0 },
  },
  chapters: {
    intent: [
      {
        key: "intent:review-history",
        label: "Review history",
        reason: "Grounded in commit: Durable Change identity.",
        explanation: "Grounded in commit: Durable Change identity. Key targets: historyProjection.",
        rows: [
          { path: "frontend/src/review/ReviewHistorySheet.tsx" },
          { path: "frontend/src/review/ReviewHistorySheet.test.tsx" },
        ],
        targets: [
          { target_id: "target-history", path: "frontend/src/review/ReviewHistorySheet.tsx", label: "historyProjection", symbol: "historyProjection", kind: "symbol", start_line: 88, end_line: 103, state: "changed_since_review", changed_since_mark: true, attention_rank: 1 },
          { target_id: "target-history-test", path: "frontend/src/review/ReviewHistorySheet.test.tsx", label: "history test", symbol: "", kind: "hunk", start_line: 70, end_line: 90, state: "reviewed", changed_since_mark: false, attention_rank: 2 },
        ],
        target_count: 2,
        progress: { target_count: 2, reviewed: 1, changed_since_review: 1, needs_changes: 0, unreviewed: 0, unknown: 0 },
        file_count: 2,
        attention_count: 1,
        reviewed_count: 1,
        changed_count: 1,
        min_attention_rank: 1,
        depends_on: [],
      },
      {
        key: "intent:review-readiness",
        label: "Review readiness",
        reason: "Grounded in commit: Durable Change identity.",
        explanation: "Grounded in commit: Durable Change identity. Key targets: readinessProjection.",
        rows: [{ path: "frontend/src/review/ReviewReadinessSheet.tsx" }],
        targets: [
          { target_id: "target-readiness", path: "frontend/src/review/ReviewReadinessSheet.tsx", label: "readinessProjection", symbol: "readinessProjection", kind: "symbol", start_line: 17, end_line: 33, state: "reviewed", changed_since_mark: false, attention_rank: 3 },
        ],
        target_count: 1,
        progress: { target_count: 1, reviewed: 1, changed_since_review: 0, needs_changes: 0, unreviewed: 0, unknown: 0 },
        file_count: 1,
        attention_count: 0,
        reviewed_count: 1,
        changed_count: 0,
        min_attention_rank: 3,
        depends_on: [],
      },
    ],
    dependency: [
      { key: "intent:review-history", label: "Review history", reason: "dependency", rows: [{ path: "frontend/src/review/ReviewHistorySheet.tsx" }], file_count: 2, attention_count: 1, reviewed_count: 1, changed_count: 1, min_attention_rank: 1, depends_on: [] },
      { key: "intent:review-readiness", label: "Review readiness", reason: "dependency", rows: [{ path: "frontend/src/review/ReviewReadinessSheet.tsx" }], file_count: 1, attention_count: 0, reviewed_count: 1, changed_count: 0, min_attention_rank: 3, depends_on: ["intent:review-history"] },
    ],
    commits: [
      { key: "commit:abc", label: "feat(review): make Change identity durable", reason: "commit", rows: [{ path: "frontend/src/review/ReviewHistorySheet.tsx" }], file_count: 12, attention_count: 1, reviewed_count: 1, changed_count: 1, min_attention_rank: 1, depends_on: [] },
    ],
  },
  provenance: { host: "claude", certainty: "exact", model: "sonnet" },
  session: { id: "review-1", ref: "r/review-1" },
  revision: { id: "rr-1", revision_number: 1, provenance_host: "claude", provenance_certainty: "exact", provenance_model: "sonnet" },
} as unknown as ReviewOverview;

describe("ReviewOverviewSheet", () => {
  beforeEach(() => {
    vi.mocked(fetchReviewVerification).mockImplementation(() => new Promise(() => {}));
    vi.mocked(fetchReviewDependencies).mockImplementation(() => new Promise(() => {}));
  });
  it("shows exact recorded sessions without claiming a single author", () => {
    render(
      <ReviewOverviewSheet
        overview={{
          ...OVERVIEW,
          attributions: [
            { scope: "revision", scope_key: "", host: "claude", session_id: "s1", role: "primary", model: "sonnet", certainty: "exact", method: "recorded_edit" },
            { scope: "file", scope_key: "src/review.ts", host: "codex", session_id: "s2", role: "contributor", model: "gpt-5.6", certainty: "exact", method: "recorded_edit" },
          ],
        }}
        onClose={vi.fn()}
        onSelectPath={vi.fn()}
      />,
    );

    expect(screen.getByText("Provenance")).toBeTruthy();
    expect(screen.getByText("2 recorded sessions")).toBeTruthy();
    expect(screen.getByText(/no single author assumed/)).toBeTruthy();
    expect(screen.getByText("Sessions")).toBeTruthy();
    expect(screen.getByText(/claude · sonnet · exact/)).toBeTruthy();
    expect(screen.getByText(/codex · gpt-5.6 · exact/)).toBeTruthy();
  });

  it("traps Tab inside the overview dialog", async () => {
    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={vi.fn()} onSelectPath={vi.fn()} />);
    const close = screen.getByRole("button", { name: "Close change overview" });
    const lastFocusable = screen.getByRole("button", { name: /feat\(review\): make Change identity durable/i });
    await waitFor(() => expect(document.activeElement).toBe(close));
    await userEvent.tab({ shift: true });
    expect(document.activeElement).toBe(lastFocusable);
    await userEvent.tab();
    expect(document.activeElement).toBe(close);
  });


  it("makes conceptual groups primary with grounded progress and exact target navigation", async () => {
    const onClose = vi.fn();
    const onSelectPath = vi.fn();
    const onSelectTarget = vi.fn();
    render(
      <ReviewOverviewSheet
        overview={OVERVIEW}
        onClose={onClose}
        onSelectPath={onSelectPath}
        onSelectTarget={onSelectTarget}
      />,
    );

    expect(screen.getByText("Major changes")).toBeTruthy();
    expect(screen.getAllByText("Review history").length).toBeGreaterThan(0);
    expect(screen.getByText(/1\/2 reviewed/)).toBeTruthy();
    expect(screen.getByText("1 changed")).toBeTruthy();
    expect(screen.getAllByText(/Grounded in commit: Durable Change identity/).length).toBeGreaterThan(0);

    await userEvent.click(screen.getByText("Dependency / commit lenses"));
    expect(screen.getByText("Dependency order")).toBeTruthy();
    expect(screen.getByText("Commits")).toBeTruthy();

    await userEvent.click(screen.getByText("2 files · 2 review areas"));
    await userEvent.click(screen.getByRole("button", { name: /historyProjection/ }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelectTarget).toHaveBeenCalledWith(
      "target-history",
      "frontend/src/review/ReviewHistorySheet.tsx",
    );
  });

  it("closes and selects a conceptual group start path", async () => {
    const onClose = vi.fn();
    const onSelectPath = vi.fn();
    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={onClose} onSelectPath={onSelectPath} />);
    await userEvent.click(screen.getByRole("button", { name: /Open Review readiness conceptual change/i }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelectPath).toHaveBeenCalledWith("frontend/src/review/ReviewReadinessSheet.tsx");
  });

  it("does not add conceptual ceremony to a small Change", () => {
    render(
      <ReviewOverviewSheet
        overview={{
          ...OVERVIEW,
          brief: { ...OVERVIEW.brief!, major_changes: [] },
          chapters: {
            intent: [OVERVIEW.chapters!.intent[0]],
            dependency: [],
            commits: [],
          },
        }}
        onClose={vi.fn()}
        onSelectPath={vi.fn()}
      />,
    );
    expect(screen.queryByText("Major changes")).toBeNull();
    expect(screen.queryByText("Dependency / commit lenses")).toBeNull();
  });

  it("shows exact Snapshot verification and semantic dependency evidence without treating stale PASS as current", async () => {
    const artifactDigest = "sha256:" + "a".repeat(64);
    vi.mocked(fetchReviewVerification).mockResolvedValueOnce({
      review_id: "review-1",
      snapshot_id: "rr-1",
      snapshot_number: 1,
      checks: [
        {
          name: "CI / unit tests",
          status: "PASS",
          state: "stale",
          snapshot_id: "rr-0",
          snapshot_number: 0,
          source: "ci",
          source_ref: "github-actions:42",
          lane: "ci",
          path: "",
          detail: "582 passed on the previous Snapshot",
          observed_at: "2026-09-24T23:00:00+00:00",
          artifact: null,
        },
        {
          name: "Browser checks",
          status: "NOT_RUN",
          state: "current",
          snapshot_id: "rr-1",
          snapshot_number: 1,
          source: "test",
          source_ref: "",
          lane: "local",
          path: "",
          detail: "browser runtime unavailable",
          observed_at: "2026-09-25T00:00:00+00:00",
          artifact: null,
        },
      ],
      summary: { pass: 0, fail: 0, not_run: 1, unknown: 0, stale: 1 },
      artifacts: [
        {
          id: "ev-sbom",
          kind: "sbom",
          title: "BuildKit SPDX",
          state: "current",
          snapshot_id: "rr-1",
          snapshot_number: 1,
          digest: "b".repeat(64),
          source: "ci",
          source_ref: "BuildKit",
          observed_at: "2026-09-25T00:00:00+00:00",
          verification_status: "",
          artifact_digest: artifactDigest,
          standard: "SPDX",
          format_version: "SPDX-2.3",
          observation_source: "BuildKit",
        },
      ],
    });
    vi.mocked(fetchReviewDependencies).mockResolvedValueOnce({
      review_id: "review-1",
      snapshot_id: "rr-1",
      snapshot_number: 1,
      supported: true,
      lockfiles: [{ path: "uv.lock", manifest: "pyproject.toml", ecosystem: "pypi", semantic_changes: 2, direct_changes: 1, transitive_changes: 1 }],
      changes: [
        {
          ecosystem: "pypi",
          identity: "cryptography",
          name: "cryptography",
          direct: true,
          scope: "runtime",
          changes: ["version"],
          before_requirement: "cryptography>=45,<46",
          after_requirement: "cryptography>=46,<47",
          before_version: "45.0.0",
          after_version: "46.0.0",
          before_source: "registry:https://pypi.org/simple",
          after_source: "registry:https://pypi.org/simple",
          before_integrity: "sha256:old",
          after_integrity: "sha256:new",
          license: "Apache-2.0",
          license_source: "sbom",
          manifest: "pyproject.toml",
          lockfile: "uv.lock",
          dependency_paths: [["cryptography"]],
          transitive_changes: 1,
          artifacts: [{ artifact_digest: artifactDigest, version: "46.0.0", license: "Apache-2.0", purl: "", sbom_evidence_id: "ev-sbom", standard: "SPDX" }],
          vulnerabilities: [{ advisory: "CVE-2099-0001", severity: "HIGH", artifact_digest: artifactDigest, source: "Artifact Analysis", observed_at: "2026-09-25T00:00:00+00:00", evidence_id: "ev-scan" }],
          target_unit_key: "dependency:pypi:cryptography",
          target_id: "target:dependency:pypi:cryptography",
        },
      ],
      summary: { semantic_changes: 1, direct_changes: 1, transitive_changes: 0, added: 0, removed: 0, version_changed: 1, source_changed: 0, integrity_changed: 1 },
      supply_chain: {
        sboms: [{ evidence_id: "ev-sbom", artifact_digest: artifactDigest, standard: "SPDX" }],
        provenance: [],
        vulnerability_scans: [{ evidence_id: "ev-scan", artifact_digest: artifactDigest }],
      },
      degraded: [],
    });

    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={vi.fn()} onSelectPath={vi.fn()} />);

    expect(await screen.findByText("stale PASS")).toBeTruthy();
    expect(screen.getByText("NOT_RUN")).toBeTruthy();
    expect(screen.getByText("Dependency review")).toBeTruthy();
    const dependencyCard = screen.getByText("cryptography").closest("article");
    expect(dependencyCard?.textContent).toContain("45.0.0 → 46.0.0");
    expect(dependencyCard?.textContent).toContain("1 transitive component changed");
    expect(screen.getByText("Apache-2.0")).toBeTruthy();
    expect(screen.getByText(/Path · cryptography/)).toBeTruthy();
    expect(screen.getByText(/Integrity\/checksum changed/)).toBeTruthy();
    expect(screen.getByText(/CVE-2099-0001 · HIGH · Artifact Analysis · 2026-09-25T00:00:00\+00:00/)).toBeTruthy();
    expect(screen.getAllByText(/sha256:aaaaaaaaaa/).length).toBeGreaterThan(0);
  });

  it("lets a human waive a stuck check with a reason and shows it as waived, not passed", async () => {
    const stuck: ReviewVerificationCheck = {
      name: "CI / unit tests",
      status: "FAIL",
      state: "stale",
      snapshot_id: "rr-0",
      snapshot_number: 0,
      source: "ci",
      source_ref: "",
      lane: "ci",
      path: "",
      detail: "",
      observed_at: "2026-09-24T23:00:00+00:00",
      artifact: null,
      waivable: true,
    };
    const projection = (check: ReviewVerificationCheck): ReviewVerificationProjection => ({
      review_id: "review-1",
      snapshot_id: "rr-1",
      snapshot_number: 1,
      checks: [check],
      summary: { pass: 0, fail: 0, not_run: 0, unknown: 0, stale: 1 },
      artifacts: [],
    });
    vi.mocked(fetchReviewVerification).mockResolvedValueOnce(projection(stuck));
    vi.mocked(fetchReviewDependencies).mockResolvedValueOnce({
      review_id: "review-1", snapshot_id: "rr-1", snapshot_number: 1, supported: false, lockfiles: [], changes: [],
      summary: { semantic_changes: 0, direct_changes: 0, transitive_changes: 0, added: 0, removed: 0, version_changed: 0, source_changed: 0, integrity_changed: 0 },
      supply_chain: { sboms: [], provenance: [], vulnerability_scans: [] },
      degraded: [],
    });
    vi.mocked(waiveReviewVerificationCheck).mockResolvedValueOnce({
      verification: projection({
        ...stuck,
        waivable: false,
        waiver: { evidence_id: "ev-w", reason: "Job retired from CI", actor: "local", snapshot_id: "rr-1", observed_at: "2026-09-25T00:00:00+00:00" },
      }),
    });
    const onWaived = vi.fn();

    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={vi.fn()} onSelectPath={vi.fn()} onWaived={onWaived} />);

    await userEvent.click(await screen.findByRole("button", { name: "Waive CI / unit tests" }));
    const record = screen.getByRole("button", { name: "Record waiver" });
    expect(record).toHaveProperty("disabled", true);
    await userEvent.type(screen.getByRole("textbox", { name: "Waiver reason for CI / unit tests" }), "Job retired from CI");
    await userEvent.click(record);

    expect(waiveReviewVerificationCheck).toHaveBeenCalledWith("review-1", {
      revision_id: "rr-1",
      name: "CI / unit tests",
      reason: "Job retired from CI",
    });
    expect(await screen.findByText("waived")).toBeTruthy();
    expect(screen.getByText(/Waived by a human: Job retired from CI/)).toBeTruthy();
    expect(screen.queryByText("PASS")).toBeNull();
    expect(screen.queryByRole("button", { name: "Waive CI / unit tests" })).toBeNull();
    expect(onWaived).toHaveBeenCalledTimes(1);
  });

  it("labels author-session evidence as unbound context instead of a Snapshot result", async () => {
    vi.mocked(fetchReviewVerification).mockResolvedValueOnce({
      review_id: "review-1",
      snapshot_id: "rr-1",
      snapshot_number: 1,
      checks: [
        {
          name: "Full suite",
          status: "PASS",
          state: "session",
          snapshot_id: "rr-1",
          snapshot_number: 1,
          source: "session:abc",
          source_ref: "",
          lane: "session:abc",
          path: "",
          detail: "pytest -q (exit 0)",
          observed_at: "2026-09-25T00:00:00+00:00",
          artifact: null,
        },
      ],
      summary: { pass: 0, fail: 0, not_run: 0, unknown: 0, stale: 0 },
      artifacts: [],
    });
    vi.mocked(fetchReviewDependencies).mockRejectedValueOnce(new Error("dependency review unavailable"));

    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={vi.fn()} onSelectPath={vi.fn()} />);

    expect(await screen.findByText("session PASS")).toBeTruthy();
    expect(screen.getByText("author session · not run against this Snapshot")).toBeTruthy();
  });

  it("shows what the browser observed with frozen before/after evidence bound to exact Snapshots", async () => {
    let objectUrl = 0;
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => `blob:browser-${++objectUrl}`) });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    vi.mocked(fetchReviewEvidenceContent).mockImplementation(async () => new Blob(["x"]));
    const artifact = (id: string, snapshot: number) => ({
      evidence_id: id,
      digest: `${id}-digest-0123456789`,
      mime_type: "image/png",
      bytes: 1,
      observed_at: "2026-09-25T00:00:00+00:00",
      snapshot_id: `rr-${snapshot}`,
      snapshot_number: snapshot,
    });
    const run = {
      evidence_id: "ev-run",
      record_digest: "record-digest-0123456789",
      snapshot_id: "rr-1",
      snapshot_number: 1,
      side: "new" as const,
      status: "FAIL" as const,
      status_reasons: ["1 assertion(s) failed", "1 console error(s)"],
      observed_at: "2026-09-25T00:00:00+00:00",
      source: "ci",
      provider: "playwright",
      run_ref: "https://ci.example.test/runs/7",
      target_url: "http://127.0.0.1:4173/checkout",
      viewport: { width: 1280, height: 800 },
      environment: { browser: "chromium" },
      completed: true,
      navigation: { status: 200 },
      assertions: [
        { name: "cart total", status: "FAIL", expected: "$10", actual: "$0", detail: "" },
        { name: "pay button", status: "PASS", expected: "", actual: "", detail: "" },
      ],
      console: [{ level: "error", text: "TypeError: cart is undefined", location: "cart.js:3" }],
      runtime_errors: [],
      visual: { diff_ratio: null, threshold: null, threshold_source: "" },
      observations: ["Agent: the page looks mostly fine."],
      artifacts: { screenshot: artifact("ev-after", 1), video: { ...artifact("ev-video", 1), mime_type: "video/webm" }, trace: { ...artifact("ev-trace", 1), mime_type: "application/zip" } },
      started_at: "",
      finished_at: "",
    };
    const baseline = {
      ...run,
      evidence_id: "ev-base-run",
      side: "old" as const,
      status: "PASS" as const,
      relation: "base" as const,
      assertions: [],
      console: [],
      observations: [],
      artifacts: { screenshot: artifact("ev-before", 1), video: { ...artifact("ev-video-before", 1), mime_type: "video/webm" } },
    };
    vi.mocked(fetchReviewVerification).mockResolvedValueOnce({
      review_id: "review-1",
      snapshot_id: "rr-1",
      snapshot_number: 1,
      checks: [
        {
          name: "Checkout regression · 1280x800",
          kind: "browser",
          status: "FAIL",
          state: "current",
          snapshot_id: "rr-1",
          snapshot_number: 1,
          source: "ci",
          source_ref: "browser:checkout:1280x800:new",
          lane: "browser",
          path: "",
          detail: "/checkout · 1/2 assertions passed",
          observed_at: "2026-09-25T00:00:00+00:00",
          artifact: null,
          required: true,
          browser: { scenario: "checkout", route: "/checkout", configured: true, applicability: "changed path", run, baseline },
        },
        {
          name: "Settings smoke",
          kind: "browser",
          status: "NOT_RUN",
          state: "current",
          snapshot_id: "rr-1",
          snapshot_number: 1,
          source: "policy",
          source_ref: "browser:settings",
          lane: "browser",
          path: "",
          detail: "Configured browser scenario applies but no result was recorded for this Snapshot.",
          observed_at: "",
          artifact: null,
          required: false,
          browser: { scenario: "settings", route: "/settings", configured: true, applicability: "changed path", run: null, baseline: null },
        },
      ],
      summary: { pass: 0, fail: 1, not_run: 1, unknown: 0, stale: 0 },
      artifacts: [],
    } as never);
    vi.mocked(fetchReviewDependencies).mockResolvedValueOnce({
      review_id: "review-1", snapshot_id: "rr-1", snapshot_number: 1, supported: false, lockfiles: [], changes: [],
      summary: { semantic_changes: 0, direct_changes: 0, transitive_changes: 0, added: 0, removed: 0, version_changed: 0, source_changed: 0, integrity_changed: 0 },
      supply_chain: { sboms: [], provenance: [], vulnerability_scans: [] }, degraded: [],
    });

    render(<ReviewOverviewSheet overview={OVERVIEW} onClose={vi.fn()} onSelectPath={vi.fn()} />);

    expect(await screen.findByText("Checkout regression · 1280x800")).toBeTruthy();
    expect(screen.getByText("required")).toBeTruthy();
    expect(screen.getByText("optional")).toBeTruthy();
    // Evidence bytes are fetched only when the reviewer expands the result.
    expect(fetchReviewEvidenceContent).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText(/What the browser observed/));
    expect(await screen.findByText(/1\/2 passed/)).toBeTruthy();
    expect(screen.getByText("cart total")).toBeTruthy();
    expect(screen.getByText(/TypeError: cart is undefined/)).toBeTruthy();
    expect(screen.getByText("Observations · not verification")).toBeTruthy();
    expect(await screen.findByTestId("image-overlay-compare")).toBeTruthy();
    expect(screen.getByAltText("Before screenshot")).toBeTruthy();
    expect(screen.getByAltText("After screenshot")).toBeTruthy();
    expect(screen.getByRole("slider", { name: "Screenshot comparison divider" })).toBeTruthy();
    expect(screen.getByLabelText("Paired browser recordings")).toBeTruthy();
    expect(screen.getByRole("button", { name: "After trace" })).toBeTruthy();
    expect(screen.getByText(/pre-change side of this Snapshot/)).toBeTruthy();
    expect(vi.mocked(fetchReviewEvidenceContent).mock.calls.map(([id]) => id)).toEqual(
      expect.arrayContaining(["ev-before", "ev-after", "ev-video-before", "ev-video"]),
    );
  });

  it("keeps surfaces as compact navigation instead of duplicating execution UI", async () => {
    const onClose = vi.fn();
    const onSelectPath = vi.fn();
    const onSelectSurface = vi.fn();
    const surfaces: ReviewSurfaceInfo[] = [{
      id: "/",
      provider: "web",
      kind: "web.route",
      title: "Storefront",
      locator: "/",
      runtime: "frontend",
      affected_paths: ["src/Hero.tsx", "src/style.css"],
      capabilities: ["preview", "compare", "execute"],
      metadata: {},
    }];

    render(
      <ReviewOverviewSheet
        overview={OVERVIEW}
        surfaces={surfaces}
        onClose={onClose}
        onSelectPath={onSelectPath}
        onSelectSurface={onSelectSurface}
      />,
    );

    expect(screen.getByText("Product surfaces")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Run" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Compare" })).toBeNull();

    const surfaceButton = screen.getByRole("button", { name: "Open / surface" });
    expect(surfaceButton.textContent).toContain("2 files");
    const majorChangeButton = screen.getByRole("button", { name: /Open Review history conceptual change/i });
    expect(majorChangeButton.compareDocumentPosition(surfaceButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await userEvent.click(surfaceButton);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onSelectSurface).toHaveBeenCalledWith("web:/");
    expect(onSelectPath).not.toHaveBeenCalled();
  });
});
