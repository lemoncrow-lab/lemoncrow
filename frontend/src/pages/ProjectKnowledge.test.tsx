import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ requestBrowserJson: vi.fn() }));
vi.mock("../review/reviewApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../review/reviewApi")>()),
  requestBrowserJson: api.requestBrowserJson,
}));

import ProjectKnowledge, { documentText } from "./ProjectKnowledge";
import type { KnowledgeEntry, KnowledgeListing, KnowledgeOverview } from "./projectKnowledgeApi";

const REPO = `repository:${"a".repeat(64)}`;
const NARRATIVE = `knowledge:${"b".repeat(64)}`;
const DOCUMENT = `knowledge:${"c".repeat(64)}`;
const BASE = `/api/project-knowledge/${encodeURIComponent(REPO)}`;

afterEach(() => {
  vi.clearAllMocks();
  // The sync-command tests spy on fetch for the indexed projects.
  vi.restoreAllMocks();
});

function source() {
  return {
    provider: "git",
    repository_key: "acme/widgets",
    canonical_ref: "refs/heads/main",
    repo_root: "/work/widgets",
    updated_at: "2026-10-07T10:00:00Z",
  };
}

function listing(overrides: Partial<KnowledgeListing> = {}): KnowledgeListing {
  return {
    available: true,
    scope: "local",
    repositories: [
      {
        repository_id: REPO,
        canonical_ref: "refs/heads/main",
        current_generation: "generation:1",
        updated_at: "2026-10-07T10:00:00Z",
        entries: { deterministic: 3, source_document: 1, ai_narrated: 1 },
        source: source(),
      },
    ],
    truncated: false,
    sync_command: "lc knowledge project sync --repo <checkout>",
    legacy_overlay_note: "The legacy overlay is not Project Knowledge.",
    ...overrides,
  };
}

function entry(overrides: Partial<KnowledgeEntry> = {}): KnowledgeEntry {
  return {
    entry_id: NARRATIVE,
    subject: { kind: "path", key: "src/billing" },
    knowledge_kind: "subject_narrative",
    narrative: "subject",
    body: "Billing charges cards through the gateway.",
    body_truncated: false,
    state: "current",
    support: "ai_narrated",
    support_label: "AI narrative",
    source_refs: ["git:blob:src/billing/charge.py@abc"],
    source_refs_omitted: 2,
    source_paths: ["src/billing/charge.py"],
    source_paths_omitted: 0,
    review_refs: [],
    merge_refs: [],
    generation: "generation:1",
    event: { kind: "merged_change", label: "Landed Change", generation_id: "generation:1", observed_at: "2026-10-06T09:00:00Z" },
    updated_at: "2026-10-07T10:00:00Z",
    ...overrides,
  };
}

function overview(overrides: Partial<KnowledgeOverview> = {}): KnowledgeOverview {
  return {
    repository: { repository_id: REPO, current_generation: "generation:1", source: source() },
    narratives: [entry()],
    narratives_truncated: false,
    context: {
      entries: [
        entry({
          entry_id: DOCUMENT,
          subject: { kind: "path", key: "docs/adr/0001.md" },
          knowledge_kind: "document",
          narrative: null,
          body: "# Payments We use the **gateway** for every charge. ## Refunds See [the runbook](docs/refunds.md).",
          support: "source_document",
          support_label: "Repository document",
          source_refs_omitted: 0,
          event: null,
        }),
      ],
      term: "",
      offset: 0,
      next_offset: 25,
    },
    evolution: [{ kind: "merged_change", label: "Landed Change", generation_id: "generation:1", observed_at: "2026-10-06T09:00:00Z" }],
    regenerate: { available: true, reason: "" },
    ...overrides,
  };
}

function serve(routes: { overview?: KnowledgeOverview; listing?: KnowledgeListing } = {}) {
  api.requestBrowserJson.mockImplementation((path: string, init: RequestInit = {}) => {
    if (path === "/api/project-knowledge") return Promise.resolve(routes.listing ?? listing());
    if (path.startsWith(BASE) && !init.method) return Promise.resolve(routes.overview ?? overview());
    if (path.endsWith("/hide")) return Promise.resolve({ status: "hidden", entry_id: NARRATIVE });
    if (path.endsWith("/regenerate")) return Promise.resolve({ status: "queued", entry_id: NARRATIVE, enqueued: true });
    if (path.endsWith("/corrections")) {
      return Promise.resolve({
        path: "docs/project-knowledge/corrections/src-billing-0123456789.md",
        content: "# Correction\n",
        patch: "diff --git a/x b/x\n+# Correction\n",
      });
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

function calls(predicate: (path: string, init: RequestInit) => boolean) {
  return api.requestBrowserJson.mock.calls.filter(([path, init = {}]) => predicate(String(path), init as RequestInit));
}

async function openActions(region: HTMLElement) {
  await userEvent.click(within(region).getAllByRole("button", { name: "Actions" })[0]);
  return screen.findByRole("menu");
}

describe("documentText", () => {
  it("turns a collapsed Markdown document into readable lines", () => {
    expect(documentText("# acme-web The storefront. ## Getting started Run `pnpm dev`, see [docs](a.md).")).toBe(
      "acme-web The storefront.\nGetting started Run pnpm dev, see docs.",
    );
    // Paths keep their underscores, and a bare # in prose is left alone.
    expect(documentText("src/__init__.py handles issue#4")).toBe("src/__init__.py handles issue#4");
  });
});

describe("ProjectKnowledge", () => {
  it("names each kind of knowledge and shows the sync command when nothing is synced", async () => {
    serve({ listing: listing({ repositories: [] }) });
    render(<ProjectKnowledge />);

    expect(await screen.findByText("Nothing synced yet")).toBeInTheDocument();
    expect(screen.getByText("lc knowledge project sync --repo <checkout>")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
    for (const term of ["Project Knowledge", "Agent Memory", "Playbooks", "Review rules"]) {
      expect(within(screen.getByLabelText("What each kind of knowledge is")).getByText(term)).toBeInTheDocument();
    }
    expect(screen.queryByText("The legacy overlay is not Project Knowledge.")).not.toBeInTheDocument();
    // Reading never asks for a repository it does not have.
    expect(calls((path) => path !== "/api/project-knowledge")).toHaveLength(0);
  });

  it("fills the sync command with this repository's checkout, name and branch, and copies it filled", async () => {
    const template = "lc knowledge project sync --repo <checkout> --repository-key <key> --canonical-ref refs/heads/<branch>";
    serve({ listing: listing({ repositories: [], sync_command: template }) });
    // The active project is a linked worktree on a feature branch; the main checkout follows main.
    vi.spyOn(globalThis, "fetch").mockImplementation((input: RequestInfo | URL) =>
      Promise.resolve(
        String(input).includes("/api/v1/code-map/projects")
          ? new Response(JSON.stringify({
            projects: [
              { project_id: "p1", root: "/work/widgets", label: "widgets", project: "widgets", project_root: "/work/widgets", branch: "main", indexed: true, active: false },
              { project_id: "p2", root: "/work/widgets-wt", label: "widgets-wt", project: "widgets", project_root: "/work/widgets", branch: "feature/x", indexed: true, active: true },
            ],
          }), { status: 200, headers: { "Content-Type": "application/json" } })
          : new Response("not found", { status: 404 }),
      ));
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<ProjectKnowledge />);

    const filled = "lc knowledge project sync --repo /work/widgets --repository-key widgets --canonical-ref refs/heads/main";
    expect(await screen.findByText(filled)).toBeInTheDocument();
    expect(screen.queryByText(/<checkout>|<key>|<branch>/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /copy/i }));
    expect(writeText).toHaveBeenCalledWith(filled);
  });

  it("keeps the server's placeholders when no indexed checkout is known", async () => {
    const template = "lc knowledge project sync --repo <checkout> --repository-key <key> --canonical-ref refs/heads/<branch>";
    serve({ listing: listing({ repositories: [], sync_command: template }) });
    vi.spyOn(globalThis, "fetch").mockImplementation(() => Promise.resolve(new Response("not found", { status: 404 })));
    render(<ProjectKnowledge />);

    expect(await screen.findByText(template)).toBeInTheDocument();
  });

  it("shows narratives with their sources, source-backed context and recent evolution", async () => {
    serve();
    render(<ProjectKnowledge />);

    expect(await screen.findByText("Billing charges cards through the gateway.")).toBeInTheDocument();
    expect(screen.getByText(/We use the gateway for every charge\./)).toHaveTextContent(
      "Payments We use the gateway for every charge. Refunds See the runbook.",
    );
    expect(screen.getByText("acme/widgets")).toBeInTheDocument();
    const narratives = screen.getByRole("region", { name: "AI narratives" });
    expect(within(narratives).getByText("AI narrative")).toBeInTheDocument();
    expect(within(narratives).getByText("git:blob:src/billing/charge.py@abc")).toBeInTheDocument();
    expect(within(narratives).getByText("and 2 more")).toBeInTheDocument();
    const context = screen.getByRole("region", { name: "Source-backed context" });
    expect(within(context).getByText("Repository document")).toBeInTheDocument();
    expect(within(context).queryByText("Document")).not.toBeInTheDocument();
    expect(within(narratives).getByText("Change landed · 2026-10-06")).toBeInTheDocument();
    const evolution = screen.getByRole("region", { name: "Recent evolution" });
    expect(within(evolution).getByText("Change landed")).toBeInTheDocument();
    expect(within(evolution).getByText("A change merged into main.")).toBeInTheDocument();
    // Source-backed cards offer only a correction; hiding is for AI narratives.
    const menu = await openActions(context);
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Propose correction"]);
    expect(calls((_path, init) => Boolean(init.method))).toHaveLength(0);
  });

  it("hides a narrative, refreshes, and offers a fresh one", async () => {
    serve();
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    await within(narratives).findByText("Billing charges cards through the gateway.");

    await userEvent.click(within(await openActions(narratives)).getByRole("menuitem", { name: "Hide" }));
    const notice = await screen.findByTestId("project-knowledge-notice");
    expect(notice).toHaveTextContent("no longer feeds Brief, Ask or AI Review");
    expect(calls((path, init) => path === `${BASE}/narratives/${encodeURIComponent(NARRATIVE)}/hide` && init.method === "POST")).toHaveLength(1);
    await waitFor(() => expect(calls((path, init) => path.startsWith(BASE) && !init.method)).toHaveLength(2));

    fireEvent.click(within(notice).getByRole("button", { name: "Regenerate a fresh narrative" }));
    await waitFor(() => expect(screen.getByTestId("project-knowledge-notice")).toHaveTextContent("Queued."));
    // Gate K: local work runs when the local worker runs it, never "always".
    expect(screen.getByTestId("project-knowledge-notice")).toHaveTextContent("when LemonCrow's local worker runs it");
    expect(
      calls((path, init) => path === `${BASE}/narratives/${encodeURIComponent(NARRATIVE)}/regenerate` && init.method === "POST"),
    ).toHaveLength(1);
  });

  it("keeps Regenerate unavailable, with the reason, when the checkout is unknown", async () => {
    serve({ overview: overview({ regenerate: { available: false, reason: "run sync from the checkout once" } }) });
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    await within(narratives).findByText("Billing charges cards through the gateway.");
    const regenerate = within(await openActions(narratives)).getByRole("menuitem", { name: /^Regenerate/ });

    expect(regenerate).toHaveAttribute("data-disabled");
    expect(regenerate).toHaveTextContent("run sync from the checkout once");
    await userEvent.click(regenerate);
    expect(calls((path) => path.endsWith("/regenerate"))).toHaveLength(0);
  });

  it("reports a refused regenerate instead of claiming it queued", async () => {
    serve();
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    await within(narratives).findByText("Billing charges cards through the gateway.");
    api.requestBrowserJson.mockImplementationOnce(() =>
      Promise.resolve({ status: "refused", reason: "content_access_unavailable", entry_id: NARRATIVE }),
    );

    await userEvent.click(within(await openActions(narratives)).getByRole("menuitem", { name: "Regenerate" }));
    expect(await screen.findByTestId("project-knowledge-notice")).toHaveTextContent(
      "Not queued: content_access_unavailable.",
    );
  });

  it("turns a correction into a patch and writes nothing itself", async () => {
    serve();
    render(<ProjectKnowledge />);
    const narratives = await screen.findByRole("region", { name: "AI narratives" });
    await within(narratives).findByText("Billing charges cards through the gateway.");

    await userEvent.click(within(await openActions(narratives)).getByRole("menuitem", { name: "Propose correction" }));
    const form = within(narratives).getByTestId("project-knowledge-correction");
    expect(within(form).getByRole("button", { name: "Create patch" })).toBeDisabled();
    fireEvent.change(within(form).getByLabelText("Correction"), { target: { value: "Refunds go through the ledger." } });
    fireEvent.click(within(form).getByRole("button", { name: "Create patch" }));

    expect(await within(form).findByText("docs/project-knowledge/corrections/src-billing-0123456789.md")).toBeInTheDocument();
    const [request] = calls((path) => path.endsWith("/corrections"));
    expect(request[0]).toBe(`${BASE}/corrections`);
    expect(JSON.parse(String((request[1] as RequestInit).body))).toEqual({
      text: "Refunds go through the ledger.",
      entry_id: NARRATIVE,
    });
  });

  it("searches and pages the source-backed context", async () => {
    serve();
    render(<ProjectKnowledge />);
    const context = await screen.findByRole("region", { name: "Source-backed context" });
    await within(context).findByText(/We use the gateway for every charge\./);

    fireEvent.change(within(context).getByLabelText("Search Project Knowledge"), { target: { value: " billing " } });
    fireEvent.click(within(context).getByRole("button", { name: "Search" }));
    await waitFor(() => expect(calls((path) => path === `${BASE}?q=billing`)).toHaveLength(1));

    fireEvent.click(await within(context).findByRole("button", { name: "More" }));
    await waitFor(() => expect(calls((path) => path === `${BASE}?offset=25`)).toHaveLength(1));
  });

  it("shows a failed read as an alert", async () => {
    api.requestBrowserJson.mockRejectedValue(new Error("Project Knowledge is unavailable"));
    render(<ProjectKnowledge />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Project Knowledge is unavailable");
  });
});
