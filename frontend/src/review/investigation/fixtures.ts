// Deterministic Review fixtures for the investigation work (N00-N06). Each one
// is built from the real projection types, so `tsc` proves it matches what the
// Reader reads; nothing here is random or time-dependent.
import type {
  EvidenceRow,
  ReviewBehaviorSummary,
  ReviewBehaviorTarget,
  ReviewDecisionState,
  ReviewProjection,
  ReviewProjectionClaim,
  ReviewProjectionDecision,
  ReviewProjectionItem,
  ReviewTarget,
  RevisionInfo,
} from "../types";

export interface InvestigationFixture {
  name: string;
  description: string;
  projection: ReviewProjection;
  targets: ReviewTarget[];
  checks: EvidenceRow[];
  revision: Pick<RevisionInfo, "id" | "base_sha" | "head_sha">;
}

export function fixtureTarget(id: string, path: string, symbol: string, overrides: Partial<ReviewTarget> = {}): ReviewTarget {
  return {
    target_id: id,
    unit_key: `unit:${id}`,
    kind: symbol ? "symbol" : "file",
    path,
    label: symbol ? `${path}::${symbol}` : path,
    symbol,
    start_line: 10,
    end_line: 30,
    hunk_ordinals: [0],
    spans: [{ side: "new", start_line: 12, end_line: 18, hunk_ordinal: 0 }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 0,
    attention_level: "normal",
    reasons: [],
    additions: 6,
    deletions: 2,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
    ...overrides,
  };
}

function priority(attention: "review_first" | "understand" | "routine" | "done", position: number): ReviewTarget["priority"] {
  const tier = { review_first: 2, understand: 4, routine: 5, done: 6 }[attention];
  return { version: 1, tier, attention, outstanding: attention !== "done", position, reasons: [] };
}

export function fixtureItem(id: string, overrides: Partial<ReviewProjectionItem> = {}): ReviewProjectionItem {
  return {
    id,
    key: id,
    kind: "area",
    title: id,
    before: "",
    after: "",
    rationale: "",
    rationale_origin: "none",
    source_label: "LemonCrow observation",
    producer: "lemoncrow",
    stale: false,
    attention: "understand",
    tier: 4,
    position: 0,
    primary_target_ids: [],
    related_target_ids: [],
    claim_ids: [],
    question_ids: [],
    concerns: [],
    unresolved_refs: [],
    ...overrides,
  };
}

export function fixtureDecisionState(overrides: Partial<ReviewDecisionState> = {}): ReviewDecisionState {
  return {
    disposition: "open",
    validity: null,
    changed_inputs: [],
    outstanding: true,
    navigable: true,
    withdrawn: false,
    trigger: null,
    trigger_fired: false,
    accepted_option: "",
    latest_event_id: "",
    latest_action: "",
    latest_reason: "",
    event_snapshot_number: null,
    reason: "decision_open",
    detail: "Needs your decision",
    tier: 3,
    limitations: [],
    ...overrides,
  };
}

export function fixtureDecision(key: string, overrides: Partial<ReviewProjectionDecision> = {}): ReviewProjectionDecision {
  return {
    key,
    producer: "claude:author",
    question: key,
    options: [{ key: "a", label: "Option A" }, { key: "b", label: "Option B" }],
    selected_option: "a",
    reason_human_required: "The requirement does not decide this.",
    assumptions: [],
    contract_scope: [],
    relied_checks: [],
    target_ids: [],
    stale: false,
    read_only: false,
    conflict: false,
    positions: [],
    actions: ["accept", "request_change", "ask", "defer"],
    state: fixtureDecisionState(),
    ...overrides,
  };
}

export function fixtureClaim(id: string, overrides: Partial<ReviewProjectionClaim> = {}): ReviewProjectionClaim {
  return {
    id,
    statement: id,
    source: "claude:author",
    target_ids: [],
    limitations: [],
    evidence: [],
    summary: "none",
    summary_text: "No evidence linked",
    result_line: "",
    ...overrides,
  };
}

export function fixtureBehaviorTarget(key: string, overrides: Partial<ReviewBehaviorTarget> = {}): ReviewBehaviorTarget {
  return {
    behavior_key: key,
    kind: "api_response",
    kind_label: "API response",
    producer: "lemoncrow",
    title: key,
    summary: "",
    change: "changed",
    severity: "medium",
    critical: false,
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    fingerprint: `fp:${key}`,
    environment_fingerprint: "env:local",
    executor_class: "author_machine",
    paths: [],
    target_ids: [],
    unverified_integrations: [],
    priority: { version: 1, tier: 3, attention: "understand", outstanding: true, position: 0, reasons: [] },
    bulk_eligible: false,
    ...overrides,
  };
}

export function fixtureBehavior(revisionId: string, overrides: Partial<ReviewBehaviorSummary> = {}): ReviewBehaviorSummary {
  const targets = overrides.targets ?? [];
  return {
    contract_version: 2,
    revision_id: revisionId,
    total: targets.length,
    progress: {
      total: targets.length,
      reviewed: 0,
      unreviewed: targets.length,
      changed_since_review: 0,
      needs_changes: 0,
      unknown: 0,
      critical: 0,
      outstanding: targets.length,
    },
    targets,
    unchanged: 0,
    execution_ran: true,
    coverage: null,
    checklist: null,
    narrative: null,
    degraded: [],
    producers: ["lemoncrow"],
    ...overrides,
  };
}

export function fixtureProjection(overrides: Partial<ReviewProjection> = {}): ReviewProjection {
  return {
    projection_version: 1,
    review_id: "review-fixture",
    revision_id: "rev-fixture",
    snapshot_number: 0,
    historical: false,
    plan: { schema_version: 1, input_digest: "digest", intents: [], limitations: [] },
    actions: [],
    questions: [],
    items: [],
    claims: [],
    decisions: [],
    coverage: {
      target_count: 0,
      author_explained: 0,
      fallback_grouped: 0,
      other: 0,
      routine: 0,
      unresolved_refs: 0,
      unexplained_targets: 0,
      file_count: 0,
    },
    counts: { review_first: 0, understand: 0, routine: 0, done: 0, decisions: 0 },
    queue: [],
    delta: null,
    ...overrides,
  };
}

function coverageFor(targets: readonly ReviewTarget[], overrides: Partial<ReviewProjection["coverage"]> = {}): ReviewProjection["coverage"] {
  return {
    target_count: targets.length,
    author_explained: 0,
    fallback_grouped: targets.length,
    other: 0,
    routine: 0,
    unresolved_refs: 0,
    unexplained_targets: 0,
    file_count: new Set(targets.map((target) => target.path)).size,
    ...overrides,
  };
}

const SHA = (seed: string) => seed.repeat(40).slice(0, 40);

/* (a) Payment retry/idempotency change; the caller that depends on it is unchanged. */
function paymentRetry(): InvestigationFixture {
  const rev = "rev-pay-1";
  const targets = [
    fixtureTarget("pay-retry", "src/payments/retry.py", "charge_with_retry", { priority: priority("review_first", 0), attention_level: "high" }),
    fixtureTarget("pay-key", "src/payments/idempotency.py", "idempotency_key", { priority: priority("review_first", 1), attention_level: "high" }),
    fixtureTarget("pay-test", "tests/payments/test_retry.py", "test_retry_reuses_key", { priority: priority("understand", 2) }),
  ];
  return {
    name: "payment-retry",
    description: "Changed payment retry and idempotency key; the unchanged checkout caller is not a Review target.",
    revision: { id: rev, base_sha: SHA("a1"), head_sha: SHA("b2") },
    targets,
    checks: [{ name: "pytest tests/payments", status: "PASS", detail: "12 passed", source: "agent", scope: "review" }],
    projection: fixtureProjection({
      review_id: "review-pay",
      revision_id: rev,
      items: [fixtureItem("item:retry", {
        kind: "behavior",
        title: "Charge retries reuse one idempotency key",
        attention: "review_first",
        primary_target_ids: ["pay-retry", "pay-key"],
        related_target_ids: ["pay-test"],
        claim_ids: ["claim:retry-key"],
        concerns: [{ scope: "retry after a successful charge", reason: "A retry that fires after the provider accepted the charge was not exercised." }],
      })],
      claims: [fixtureClaim("claim:retry-key", {
        statement: "A retried charge reuses the original idempotency key.",
        target_ids: ["pay-retry", "pay-key"],
        summary: "inferred",
        summary_text: "Inferred from the code; no linked check exercises it",
      })],
      questions: [{
        id: "q:ai:double-charge",
        reason: "ai_hypothesis",
        title: "Can a timeout retry double-charge?",
        detail: "The retry loop does not check whether the first attempt succeeded.",
        item_ids: ["item:retry"],
        target_ids: ["pay-retry"],
        actions: ["inspect_evidence", "ask"],
        tier: 3,
        origin: "ai",
      }],
      decisions: [fixtureDecision("retry-limit", {
        question: "How many times may a charge retry?",
        options: [{ key: "three", label: "Three attempts" }, { key: "five", label: "Five attempts" }],
        selected_option: "three",
        target_ids: ["pay-retry"],
      })],
      behavior: fixtureBehavior(rev, {
        targets: [fixtureBehaviorTarget("behavior:charge", {
          title: "POST /charges retries on timeout",
          target_ids: ["pay-retry"],
          paths: ["src/payments/retry.py"],
          unverified_integrations: ["payment provider"],
        })],
        coverage: {
          runs: 1,
          changed_targets: 3,
          exercised: 2,
          never_executed_count: 1,
          never_executed: [{ target_id: "pay-key", path: "src/payments/idempotency.py", label: "idempotency_key" }],
        },
      }),
      coverage: coverageFor(targets),
      counts: { review_first: 1, understand: 0, routine: 0, done: 0, decisions: 1 },
      queue: targets.map((target) => target.target_id),
    }),
  };
}

/* (b) Auth token refresh with a concurrent request and an error branch. */
function authRefresh(): InvestigationFixture {
  const rev = "rev-auth-1";
  const targets = [
    fixtureTarget("auth-refresh", "src/auth/refresh.ts", "refreshToken", { priority: priority("review_first", 0), attention_level: "high" }),
    fixtureTarget("auth-error", "src/auth/refresh.ts", "handleRefreshError", {
      priority: priority("review_first", 1),
      attention_level: "high",
      verification: { pass: 0, fail: 1, unknown: 0 },
    }),
    fixtureTarget("auth-client", "src/http/client.ts", "withAuth", {
      priority: priority("understand", 2),
      annotation_counts: { open: 2, orphaned: 0, addressed_needs_rereview: 0 },
    }),
  ];
  return {
    name: "auth-refresh",
    description: "Token refresh with a concurrent-request concern, a failing error-branch check and open human comments.",
    revision: { id: rev, base_sha: SHA("c3"), head_sha: SHA("d4") },
    targets,
    checks: [
      { name: "vitest auth/refresh error branch", status: "FAIL", detail: "expected logout, got retry", source: "agent", scope: "review" },
      { name: "playwright login", status: "NOT_RUN", detail: "", source: "policy", scope: "review" },
    ],
    projection: fixtureProjection({
      review_id: "review-auth",
      revision_id: rev,
      items: [fixtureItem("item:refresh", {
        kind: "behavior",
        title: "Expired tokens refresh once per burst",
        attention: "review_first",
        primary_target_ids: ["auth-refresh", "auth-error"],
        related_target_ids: ["auth-client"],
        claim_ids: ["claim:single-refresh"],
        concerns: [{ scope: "concurrent refresh", reason: "Two requests refreshing at once were not exercised." }],
      })],
      claims: [fixtureClaim("claim:single-refresh", {
        statement: "Concurrent requests share one refresh.",
        target_ids: ["auth-refresh"],
        summary: "partial",
        summary_text: "Partial evidence",
        evidence: [{
          key: "vitest auth/refresh",
          title: "vitest auth/refresh",
          status: "PASS",
          state: "current",
          snapshot_number: 0,
          mapping: "explicit",
          relevance: "Cited by the author",
        }],
      })],
      coverage: coverageFor(targets),
      counts: { review_first: 1, understand: 0, routine: 0, done: 0, decisions: 0 },
      queue: targets.map((target) => target.target_id),
    }),
  };
}

/* (c) A cross-service API/storage route whose index covers only part of the change. */
function crossServicePartialIndex(): InvestigationFixture {
  const rev = "rev-route-1";
  const loaded = [
    fixtureTarget("route-handler", "services/api/routes/orders.py", "create_order", { priority: priority("review_first", 0), attention_level: "high" }),
    fixtureTarget("route-client", "services/api/clients/storage.py", "put_order", { priority: priority("understand", 1) }),
    fixtureTarget("route-store", "services/storage/handlers.go", "PutOrder", { priority: priority("understand", 2) }),
  ];
  return {
    name: "cross-service-partial-index",
    description: "API route calling a storage service; only 3 of 6 targets are loaded and the static register covers changed files only.",
    revision: { id: rev, base_sha: SHA("e5"), head_sha: SHA("f6") },
    targets: loaded,
    checks: [{ name: "pytest services/api", status: "PASS", detail: "", source: "ci", scope: "review" }],
    projection: fixtureProjection({
      review_id: "review-route",
      revision_id: rev,
      items: [fixtureItem("item:route", {
        title: "Orders route writes through the storage service",
        attention: "review_first",
        primary_target_ids: ["route-handler", "route-client", "route-store", "route-unloaded"],
      })],
      behavior: fixtureBehavior(rev, {
        targets: [fixtureBehaviorTarget("behavior:orders-api", {
          title: "POST /orders now calls storage PutOrder",
          target_ids: ["route-handler"],
          paths: ["services/api/routes/orders.py"],
          unverified_integrations: ["storage service"],
        })],
        coverage: { runs: 1, changed_targets: 6, exercised: 6, never_executed_count: 0, never_executed: [] },
        degraded: ["Code graph covers 2 of 5 services"],
        static_inventory: {
          status: "ran",
          reason: "",
          register: { state: "changed_files_only", files_scanned: 3 },
          register_delta: {},
          degraded: [],
          files_analyzed: 3,
          targets: 3,
        },
      }),
      coverage: coverageFor(loaded, { target_count: 6, unresolved_refs: 1, file_count: 5 }),
      counts: { review_first: 1, understand: 0, routine: 0, done: 0, decisions: 0 },
      queue: loaded.map((target) => target.target_id),
    }),
  };
}

/* (d) A small clean change with no graph: everything that can be measured was, and passed. */
function cleanNoGraph(): InvestigationFixture {
  const rev = "rev-clean-1";
  const targets = [
    fixtureTarget("clean-fmt", "src/util/format.py", "format_amount", { state: "reviewed", reviewed_revision_id: rev, priority: priority("done", 0), verification: { pass: 1, fail: 0, unknown: 0 } }),
  ];
  return {
    name: "clean-no-graph",
    description: "One reviewed function, an observed passing check, every changed target exercised, no decisions, first Snapshot.",
    revision: { id: rev, base_sha: SHA("07"), head_sha: SHA("18") },
    targets,
    checks: [{ name: "pytest tests/util", status: "PASS", detail: "4 passed", source: "ci", scope: "review" }],
    projection: fixtureProjection({
      review_id: "review-clean",
      revision_id: rev,
      items: [fixtureItem("item:format", { title: "Amount formatting", attention: "done", primary_target_ids: ["clean-fmt"], claim_ids: ["claim:format"] })],
      claims: [fixtureClaim("claim:format", {
        statement: "Amounts keep two decimals.",
        target_ids: ["clean-fmt"],
        summary: "observed",
        summary_text: "Observed in a recorded run",
        evidence: [{ key: "pytest tests/util", title: "pytest tests/util", status: "PASS", state: "current", snapshot_number: 0, mapping: "explicit", relevance: "Cited by the author" }],
      })],
      behavior: fixtureBehavior(rev, {
        coverage: { runs: 1, changed_targets: 1, exercised: 1, never_executed_count: 0, never_executed: [] },
      }),
      coverage: coverageFor(targets),
      counts: { review_first: 0, understand: 0, routine: 0, done: 1, decisions: 0 },
      queue: [],
    }),
  };
}

/* (e) 420 targets across 30 files; a few failing checks and many review-first targets. */
function largeReview(): InvestigationFixture {
  const rev = "rev-large-1";
  const targets = Array.from({ length: 420 }, (_, index) => {
    const file = index % 30;
    const first = file < 6;
    return fixtureTarget(`large-${index}`, `src/large/module_${String(file).padStart(2, "0")}.py`, `handler_${index}`, {
      priority: priority(first ? "review_first" : "understand", index),
      attention_level: first ? "high" : "normal",
      verification: { pass: 0, fail: index % 97 === 0 ? 1 : 0, unknown: index % 61 === 0 ? 1 : 0 },
    });
  });
  return {
    name: "large-420",
    description: "420 targets over 30 files; six files are review-first.",
    revision: { id: rev, base_sha: SHA("29"), head_sha: SHA("3a") },
    targets,
    checks: [{ name: "pytest", status: "PASS", detail: "", source: "ci", scope: "review" }],
    projection: fixtureProjection({
      review_id: "review-large",
      revision_id: rev,
      items: Array.from({ length: 30 }, (_, file) => fixtureItem(`item:module-${file}`, {
        title: `Module ${file}`,
        attention: file < 6 ? "review_first" : "understand",
        position: file,
        primary_target_ids: targets.filter((_, index) => index % 30 === file).map((target) => target.target_id),
      })),
      coverage: coverageFor(targets),
      counts: { review_first: 6, understand: 24, routine: 0, done: 0, decisions: 0 },
      queue: targets.map((target) => target.target_id),
    }),
  };
}

const BILLING_TARGETS = (rev: string, changed: boolean) => [
  fixtureTarget("bill-round", "src/billing/rounding.py", "round_total", changed
    ? { state: "changed_since_review", changed_since_mark: true, reviewed_revision_id: "rev-bill-1", movement: "authored", priority: priority("review_first", 0) }
    : { state: "reviewed", reviewed_revision_id: rev, priority: priority("done", 0) }),
  fixtureTarget("bill-currency", "src/billing/currency.py", "currency_for", {
    state: "reviewed",
    reviewed_revision_id: "rev-bill-1",
    movement: "unchanged",
    priority: priority("done", 1),
  }),
];

function billingProjection(rev: string, snapshot: number, superseded: boolean): ReviewProjection {
  const accepted = fixtureDecisionState({ disposition: "accepted", validity: "current", outstanding: false, accepted_option: "a" });
  return fixtureProjection({
    review_id: "review-billing",
    revision_id: rev,
    snapshot_number: snapshot,
    items: [fixtureItem("item:billing", { title: "Billing totals", primary_target_ids: ["bill-round", "bill-currency"], claim_ids: ["claim:round"] })],
    claims: [fixtureClaim("claim:round", {
      statement: "Totals round half-even.",
      target_ids: ["bill-round"],
      summary: superseded ? "partial" : "observed",
      summary_text: superseded ? "Evidence is from an earlier Snapshot" : "Observed in a recorded run",
      evidence: [{
        key: "pytest tests/billing",
        title: "pytest tests/billing",
        status: "PASS",
        state: superseded ? "stale" : "current",
        snapshot_number: 0,
        mapping: "explicit",
        relevance: "Cited by the author",
      }],
    })],
    decisions: [
      fixtureDecision("rounding-mode", {
        question: "Which rounding mode do invoices use?",
        target_ids: ["bill-round"],
        actions: superseded ? ["reconfirm", "reopen", "ask"] : ["reopen", "ask"],
        state: superseded
          ? fixtureDecisionState({ disposition: "accepted", validity: "implementation_changed_since_acceptance", outstanding: true, accepted_option: "a", changed_inputs: ["round_total changed"] })
          : accepted,
      }),
      fixtureDecision("currency-source", {
        question: "Where does the currency come from?",
        target_ids: ["bill-currency"],
        actions: ["reopen", "ask"],
        state: accepted,
      }),
    ],
    delta: superseded
      ? { since_snapshot: 0, added: [], changed: ["bill-round"], removed: 0, stale_evidence: ["pytest tests/billing"], reopened_decisions: ["rounding-mode"] }
      : null,
    coverage: coverageFor(BILLING_TARGETS(rev, superseded)),
    counts: { review_first: superseded ? 1 : 0, understand: 0, routine: 0, done: superseded ? 1 : 2, decisions: 2 },
    queue: superseded ? ["bill-round"] : [],
  });
}

/* (f-before) Snapshot 0: both billing decisions accepted, both targets reviewed, evidence current. */
function billingBefore(): InvestigationFixture {
  const rev = "rev-bill-1";
  return {
    name: "billing-snapshot-0",
    description: "Snapshot 0 of the billing change, fully judged.",
    revision: { id: rev, base_sha: SHA("4b"), head_sha: SHA("5c") },
    targets: BILLING_TARGETS(rev, false),
    checks: [{ name: "pytest tests/billing", status: "PASS", detail: "", source: "ci", scope: "review" }],
    projection: billingProjection(rev, 0, false),
  };
}

/* (f) Snapshot 1 supersedes the rounding evidence and reopens only the rounding decision. */
function billingSuperseded(): InvestigationFixture {
  const rev = "rev-bill-2";
  return {
    name: "billing-snapshot-1",
    description: "Snapshot 1 changes round_total: its decision and evidence reopen; the currency decision stays accepted.",
    revision: { id: rev, base_sha: SHA("4b"), head_sha: SHA("6d") },
    targets: BILLING_TARGETS(rev, true),
    checks: [{ name: "pytest tests/billing", status: "PASS", detail: "", source: "ci", scope: "review" }],
    projection: billingProjection(rev, 1, true),
  };
}

export const INVESTIGATION_FIXTURES = {
  paymentRetry,
  authRefresh,
  crossServicePartialIndex,
  cleanNoGraph,
  largeReview,
  billingBefore,
  billingSuperseded,
} as const;
