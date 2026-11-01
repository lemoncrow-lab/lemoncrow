import { describe, expect, it } from "vitest";

import { investigationContext } from "../investigation/investigationContext";
import type { InvestigationFixture } from "../investigation/fixtures";
import { authRefreshWithConsumers, fixtureGroup, fixtureSite } from "./fixtures";
import { MAX_VISITS, scenariosFor, whatIfModel, type WhatIfScenarioKey } from "./whatIfModel";

function run(fixture: InvestigationFixture, targetId: string, scenario: WhatIfScenarioKey = "returns_nullable") {
  const context = investigationContext(fixture);
  return whatIfModel({ context, projection: fixture.projection, targets: fixture.targets, targetId, scenario });
}

describe("whatIfModel", () => {
  it("offers a fixed menu by target kind", () => {
    expect(scenariosFor({ kind: "symbol", symbol: "refreshToken" }).map((row) => row.key)).toEqual([
      "returns_nullable", "signature_changes", "raises_error", "becomes_async", "removed",
    ]);
    expect(scenariosFor({ kind: "symbol", symbol: "auth.Session" }).map((row) => row.key)).toEqual(["field_removed", "signature_changes", "removed"]);
    expect(scenariosFor({ kind: "document_section", symbol: "Module data · LIMIT" })[0].key).toBe("value_changes");
  });

  it("separates simulated effects on unchanged callers from actual changed edges", () => {
    const fixture = authRefreshWithConsumers({ index_status: "stale" });
    const result = run(fixture, "auth-refresh");
    expect(result.snapshotIdentity).toBe("review-auth@rev-auth-1");
    expect(result.rows.map((row) => [row.path, row.origin, row.unchanged, row.exactness])).toEqual([
      ["src/api/session.ts", "simulated", true, "snapshot"],
      ["src/jobs/keepalive.ts", "simulated", true, "snapshot"],
      ["src/http/client.ts", "actual", false, "snapshot"],
    ]);
    expect(result.rows[0].reason).toBe("Calls refreshToken; if return value becomes nullable, it would need to handle a missing (null/None) result.");
    // The in-patch caller is a changed target, so it opens in source; unchanged ones open the changed code they depend on.
    expect(result.rows[2].openTargetId).toBe("auth-client");
    expect(result.rows[0]).toMatchObject({ openTargetId: null, fromTargetId: "auth-refresh" });
    expect(result.rows[1].uncertainty).toContain("2 definitions named tick");
    // The index is stale: the list may be partial, and dynamic dispatch is always unknown.
    expect(result.complete).toBe(false);
    expect(result.gaps.map((gap) => gap.key)).toEqual(["index:stale", "candidates", "dynamic"]);
    expect(result.targetChecks).toEqual([{ key: "vitest auth/refresh", status: "PASS", proof: "snapshot" }]);
  });

  it("walks through a changed caller to its own callers, bounded and deterministic", () => {
    const fixture = authRefreshWithConsumers();
    fixture.projection.context!.consumers.push(
      fixtureGroup("src/http/client.ts", "withAuth", ["auth-client"], [fixtureSite("src/pages/home.ts", 3, "home.load")]),
    );
    const result = run(fixture, "auth-refresh");
    expect(result.rows.find((row) => row.path === "src/pages/home.ts")).toMatchObject({ depth: 2, origin: "simulated" });
    expect(run(fixture, "auth-refresh")).toEqual(result);

    const wide = authRefreshWithConsumers();
    wide.projection.context!.consumers = [fixtureGroup("src/auth/refresh.ts", "refreshToken", ["auth-refresh"],
      Array.from({ length: MAX_VISITS + 9 }, (_, index) => fixtureSite(`src/c${index}.ts`, 1, `c${index}`)), { omitted: 4, unlisted_callers: 2 })];
    const bounded = run(wide, "auth-refresh");
    expect(bounded.rows).toHaveLength(MAX_VISITS);
    expect(bounded.omitted).toBe(9 + 4);
    expect(bounded.gaps.some((gap) => gap.key === "unlisted")).toBe(true);
  });

  it("says no known callers is not no callers when the index is incomplete", () => {
    const fixture = authRefreshWithConsumers({ index_status: "absent", degraded: ["symbol_relations"] });
    fixture.projection.context!.consumers = [];
    const result = run(fixture, "auth-refresh");
    expect(result.rows).toEqual([]);
    expect(result.complete).toBe(false);
    expect(result.gaps.map((gap) => gap.title)).toEqual(["No code index", "Caller relations unavailable", "Dynamic dispatch and reflection not traced"]);

    const fresh = authRefreshWithConsumers();
    fresh.projection.context!.consumers = [];
    expect(run(fresh, "auth-refresh").complete).toBe(true);

    const none = authRefreshWithConsumers();
    none.projection = { ...none.projection, context: undefined };
    expect(run(none, "auth-refresh")).toMatchObject({ measured: false, rows: [] });
  });

  it("ties checks to the target and names name-matched checks as inferred", () => {
    const fixture = authRefreshWithConsumers();
    fixture.checks = [{ name: "vitest session renew", status: "PASS", detail: "", source: "agent", scope: "review" }];
    const row = run(fixture, "auth-refresh").rows.find((item) => item.path === "src/api/session.ts");
    expect(row?.coverage).toEqual([{ key: "vitest session renew", status: "PASS", proof: "inferred" }]);
  });
});
