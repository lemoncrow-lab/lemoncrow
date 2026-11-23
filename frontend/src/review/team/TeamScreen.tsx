import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { isColdLoad } from "../reviewBootLocation";
import { SettingsBar } from "../settings/settingsShell";
import { fetchTeamHealth, NO_REPOS, NOT_A_LEAD, repoRows, TEAM_FOOTER, TEAM_LEDE, TEAM_TITLE, type TeamHealth } from "./teamModel";

type Loaded = { state: "loading" } | { state: "not_lead" } | { state: "failed" } | { state: "ready"; health: TeamHealth };

/**
 * `/team` (SPEC 4.19, POL-3). Hosted only; the leads (organization admins) see one card per repository and never a
 * name or a count per person: the server's answer carries none to show.
 */
export function TeamScreen({ onAbsent, backTo }: { onAbsent: () => void; backTo: string }) {
  const navigate = useNavigate();
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const heading = useRef<HTMLHeadingElement>(null);
  const absent = useRef(onAbsent);
  absent.current = onAbsent;
  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const health = await fetchTeamHealth();
        if (!live) return;
        if (health === null) absent.current();
        else setLoaded({ state: "ready", health });
      } catch (reason) {
        if (live) setLoaded((reason as { status?: number }).status === 403 ? { state: "not_lead" } : { state: "failed" });
      }
    })();
    return () => { live = false; };
  }, []);
  useEffect(() => { if (loaded.state !== "loading" && !isColdLoad()) heading.current?.focus({ preventScroll: true }); }, [loaded.state]);

  return (
    <div className="mx-auto w-full max-w-5xl px-4 pb-16" data-testid="team-health">
      <SettingsBar name="Team" onBack={() => navigate(backTo)} />
      <div className="mt-4 space-y-4">
        <div>
          <h1 ref={heading} tabIndex={-1} className="text-title font-semibold outline-none">{TEAM_TITLE}</h1>
          {loaded.state === "ready" ? <p className="text-label text-muted-foreground">{TEAM_LEDE}</p> : null}
        </div>
        {loaded.state === "loading" ? <p className="text-label">Reading team health…</p> : null}
        {loaded.state === "not_lead" ? <p className="text-label" data-testid="team-not-lead">{NOT_A_LEAD}</p> : null}
        {loaded.state === "failed" ? <p role="alert" className="text-label">Couldn't read team health.</p> : null}
        {loaded.state === "ready" ? (
          loaded.health.repositories.length === 0 ? <p className="text-label">{NO_REPOS}</p> : (
            <ul className="grid gap-4 md:grid-cols-2" aria-label="Repositories">
              {loaded.health.repositories.map((repo) => (
                <li key={repo.repo_id} className="hunk" data-testid="team-repo">
                  <div className="hunk-head"><h2 className="hunk-label">{repo.repo}</h2></div>
                  <dl className="space-y-2 p-4">
                    {repoRows(repo).map((row) => (
                      <div key={row.label} className="flex flex-wrap items-baseline justify-between gap-2">
                        <dt className="text-label text-muted-foreground">{row.label}</dt>
                        <dd className="text-label font-semibold">{row.value}</dd>
                      </div>
                    ))}
                  </dl>
                </li>
              ))}
            </ul>
          )
        ) : null}
        {loaded.state === "ready" ? <p className="text-meta text-muted-foreground" data-testid="team-footer">{TEAM_FOOTER}</p> : null}
      </div>
    </div>
  );
}
