// Test fixtures for the projection's `context` block (F7): known consumers of
// changed targets and the impact pass's readiness. Shared by the Blind spots and
// What if model tests; built on the auth-refresh investigation fixture.
import { INVESTIGATION_FIXTURES, type InvestigationFixture } from "../investigation/fixtures";
import type { ReviewConsumerGroup, ReviewConsumerSite, ReviewImpactReadiness, ReviewProjectionContext } from "../types";

export function fixtureSite(path: string, line: number, symbol: string, overrides: Partial<ReviewConsumerSite> = {}): ReviewConsumerSite {
  return {
    path,
    line,
    symbol,
    snippet: "",
    kind: "caller",
    impact_kind: "untouched_caller",
    in_patch: false,
    exactness: "snapshot",
    uncertainty: "",
    inspected_by_agent: null,
    ...overrides,
  };
}

export function fixtureReadiness(overrides: Partial<ReviewImpactReadiness> = {}): ReviewImpactReadiness {
  return {
    source: "snapshot_packet",
    impact_pass: "ran",
    index_status: "fresh",
    degraded: [],
    caller_expansion: { max_symbols: 20, callers_per_symbol: 20 },
    sites_recorded: 0,
    ...overrides,
  };
}

export function fixtureGroup(path: string, symbol: string, targetIds: string[], sites: ReviewConsumerSite[], overrides: Partial<ReviewConsumerGroup> = {}): ReviewConsumerGroup {
  return {
    key: `${path}::${symbol}`,
    path,
    symbol,
    target_ids: targetIds,
    join: targetIds.length === 1 ? "exact" : "file",
    known_callers: sites.filter((site) => site.kind === "caller").length,
    sites,
    omitted: 0,
    unlisted_callers: 0,
    ...overrides,
  };
}

/** refreshToken has two unchanged callers and one in-patch caller; withAuth a contract consumer in web. */
export function authRefreshWithConsumers(readiness: Partial<ReviewImpactReadiness> = {}): InvestigationFixture {
  const base = INVESTIGATION_FIXTURES.authRefresh();
  const context: ReviewProjectionContext = {
    readiness: fixtureReadiness({ sites_recorded: 4, ...readiness }),
    consumers: [
      fixtureGroup("src/auth/refresh.ts", "refreshToken", ["auth-refresh"], [
        fixtureSite("src/api/session.ts", 12, "session.renew"),
        fixtureSite("src/jobs/keepalive.ts", 0, "keepalive.tick", { uncertainty: "2 definitions named tick in this repo" }),
        fixtureSite("src/http/client.ts", 40, "withAuth", { in_patch: true }),
      ]),
      fixtureGroup("src/http/client.ts", "", ["auth-client"], [
        fixtureSite("web/app.ts", 7, "", { kind: "consumer", impact_kind: "contract_literal", snippet: "fetch('/auth', withAuth)" }),
      ], { join: "file" }),
    ],
    consumers_omitted: 0,
  };
  return { ...base, projection: { ...base.projection, context } };
}
