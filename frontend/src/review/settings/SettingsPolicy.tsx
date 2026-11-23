import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { Button } from "../../components/ui/button";
import { fetchOverview, fetchReviewDirectory, requestBrowserJson } from "../reviewApi";
import type { ReviewPolicyProjection } from "../types";
import { alwaysADecision, commitLine, latestReview, pendingPolicyChanges, repoLabel, type PendingPolicyChange, type PolicyHistory } from "./policyModel";

type Loaded =
  | { state: "loading" }
  | { state: "none" }
  | { state: "failed" }
  | { state: "ready"; repo: string; policy: ReviewPolicyProjection | null; pending: PendingPolicyChange[]; history: PolicyHistory | "unavailable" };

/** Settings › Review policy, read-only: the rules of the repository whose review changed most recently (SPEC 4.11, 7.1 P5-4). */
export function SettingsPolicy() {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const page = await fetchReviewDirectory({ status: "all", query: "", cursor: "", limit: 20 });
        const row = latestReview(page.reviews);
        if (!row) return active && setLoaded({ state: "none" });
        const overview = await fetchOverview(row.id);
        // The history is the checkout's own git log; a server that holds no checkout has none to give.
        const history = await requestBrowserJson<PolicyHistory>(`/api/reviews/${encodeURIComponent(row.id)}/policy-history`).catch(() => "unavailable" as const);
        if (active) setLoaded({ state: "ready", repo: repoLabel(row), policy: overview.policy ?? null, pending: pendingPolicyChanges(page.reviews), history });
      } catch {
        if (active) setLoaded({ state: "failed" });
      }
    })();
    return () => { active = false; };
  }, []);

  const areas = loaded.state === "ready" ? alwaysADecision(loaded.policy) : [];
  return (
    <section id="policy" aria-labelledby="policy-title" className="scroll-mt-4 space-y-3">
      <div>
        <h2 id="policy-title" className="text-title font-semibold">Review policy</h2>
        <p className="text-label text-muted-foreground">
          For repo admins. Rules the whole team shares{loaded.state === "ready" && loaded.repo ? ` for ${loaded.repo}` : ""}, stored in the repo and reviewed like code.
        </p>
      </div>
      {loaded.state === "loading" ? <p className="text-label" data-testid="settings-policy-note">Reading the rules…</p> : null}
      {loaded.state === "failed" ? <p role="alert" className="text-label" data-testid="settings-policy-note">Couldn't read the rules.</p> : null}
      {loaded.state === "none" ? <p className="text-label" data-testid="settings-policy-note">No review has been opened yet, so there are no rules to show.</p> : null}
      {loaded.state === "ready" ? (
        <div className="hunk" data-testid="settings-policy-areas">
          <div className="border-b border-border px-4 py-3"><h3 className="text-label font-semibold text-foreground">Always a decision</h3></div>
          <div className="space-y-2 p-4">
            <p className="text-label text-muted-foreground">Areas that always need a person. Each one says why.</p>
            {areas.length === 0 ? <p className="text-label">No area is marked critical in <code>.lemoncrow/review.yaml</code>.</p> : (
              <ul className="space-y-2">
                {areas.map((area) => <li key={area.paths} className="text-label"><code>{area.paths}</code> <span className="text-muted-foreground">· {area.why}</span></li>)}
              </ul>
            )}
            {loaded.policy && loaded.policy.authority.errors.length > 0 ? <p role="alert" className="text-label text-p1">{loaded.policy.authority.errors.join(" ")}</p> : null}
            <p className="text-meta text-muted-foreground">Read from <code>.lemoncrow/review.yaml</code> at the trusted base. Edits are made in the repo.</p>
          </div>
        </div>
      ) : null}
      {loaded.state === "ready" ? (
        <>
          <div className="hunk" data-testid="settings-policy-history">
            <div className="border-b border-border px-4 py-3"><h3 className="text-label font-semibold text-foreground">History</h3></div>
            <div className="space-y-2 p-4">
              {loaded.history === "unavailable" || !loaded.history.available ? (
                <p className="text-label text-muted-foreground">The history of <code>.lemoncrow/review.yaml</code> can't be read from here.</p>
              ) : loaded.history.entries.length === 0 ? (
                <p className="text-label">No commit has changed <code>.lemoncrow/review.yaml</code> on the base branch.</p>
              ) : (
                <ul className="space-y-1">{loaded.history.entries.map((entry) => <li key={entry.commit} className="font-mono text-meta">{commitLine(entry)}</li>)}</ul>
              )}
            </div>
          </div>
          {loaded.pending.length > 0 ? (
            <div className="hunk" data-testid="settings-policy-pending">
              <div className="border-b border-border px-4 py-3"><h3 className="text-label font-semibold text-foreground">Pending changes</h3></div>
              <ul className="space-y-1 p-4">
                {loaded.pending.map((change) => (
                  <li key={change.id} className="text-label"><Link className="underline underline-offset-2" to={change.href}>{change.title}</Link> <span className="text-muted-foreground">· pending review</span></li>
                ))}
              </ul>
            </div>
          ) : null}
        </>
      ) : null}
      <Button asChild variant="outline" size="sm"><Link to="/reviews">Open your reviews</Link></Button>
    </section>
  );
}
