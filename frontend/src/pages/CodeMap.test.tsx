import { Button as UiButton } from "../components/ui/button";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import CodeMap from "./CodeMap";
import { LOCAL_DASHBOARD_PRODUCT_CAPABILITIES, ProductCapabilitiesProvider } from "../lib/RuntimeCapabilities";

vi.mock("../components/code/ReadOnlyCodeViewer", () => ({
  default: ({ value, selectedRange, changedLines, onSelectionChange }: any) => (
    <pre data-testid="highlighted-source" data-changed={JSON.stringify(changedLines ?? null)}>
      <UiButton unstyled type="button" onClick={() => onSelectionChange?.({ start: 2, end: 3, text: "export function chargeCard() {" })}>Select code</UiButton>
      {String(value)
        .split("\n")
        .map((line, index) => {
          const lineNumber = index + 1;
          const selected = selectedRange && lineNumber >= selectedRange[0] && lineNumber <= selectedRange[1];
          return (
            <span key={lineNumber} data-code-line={lineNumber} className={selected ? "border-cyan-400 bg-cyan-400/10" : ""}>
              {line || " "}
            </span>
          );
        })}
    </pre>
  ),
}));

function HistoryProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location-path">{location.pathname}</output>
      <output data-testid="location-search">{location.search}</output>
      <UiButton unstyled type="button" onClick={() => navigate(-1)}>
        Browser back
      </UiButton>
    </>
  );
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

const paymentFile = {
  id: "file::payment",
  label: "payment.ts",
  qualified_name: "src/payment.ts",
  path: "src/payment.ts",
  kind: "source",
  language: "TypeScript",
  file_type: "source",
  module: "src",
  node_type: "file",
  line: 1,
  end_line: 24,
};
const checkoutFile = { ...paymentFile, id: "file::checkout", label: "checkout.ts", qualified_name: "src/checkout.ts", path: "src/checkout.ts" };
const charge = {
  id: "charge",
  label: "chargeCard",
  qualified_name: "PaymentGateway.chargeCard",
  path: "src/payment.ts",
  kind: "method",
  language: "TypeScript",
  node_type: "symbol",
  line: 2,
  end_line: 3,
};

const context = { source: "current_index", backend: "native", exactness: "current_index", fallback: false, readiness: { backend: "native", graph: { state: "ready" } } };

function relationships(subject: Record<string, unknown>, usedBy: string[], uses: string[]) {
  const row = (path: string, kind: string) => ({ path, kind, kinds: { [kind]: 2 }, weight: 2, symbols: ["chargeCard"], lines: [7] });
  return {
    subject,
    used_by: usedBy.map((path) => row(path, "calls")),
    uses: uses.map((path) => row(path, "imports")),
    used_by_total: usedBy.length,
    uses_total: uses.length,
    filtered: { cross_language: 1 },
    truncated: false,
    degraded_reason: "",
    uses_attributed: true,
    context,
  };
}

const changes = {
  project: { root: "/repo", label: "repo" },
  files: [
    { path: "src/payment.ts", status: "M", old_path: "", additions: 4, deletions: 1, sessions: ["claude:s1"], shared: false },
    { path: "src/checkout.ts", status: "A", old_path: "", additions: 9, deletions: 0, sessions: [], shared: false },
  ],
  sessions: [
    { id: "claude:s1", host: "claude", session_id: "s1", model: "opus", title: "Tighten retries", started_at: "", files: ["src/payment.ts"], shared_files: [] },
  ],
  unattributed: ["src/checkout.ts"],
  total: 2,
  rule: "",
};

const modules = {
  project: { root: "/repo", label: "repo" },
  level: "module",
  total_files: 40,
  modules: [
    { id: "src", path: "src", label: "src", parent: "", kind: "module", files: 30, symbols: 120, changed: 0, purpose: "Payments core.", purpose_source: "src/README.md", purpose_kind: "declared", collapsed: false, package: false, top_files: ["src/payment.ts"] },
    { id: "web", path: "web", label: "web", parent: "", kind: "module", files: 10, symbols: 20, changed: 0, purpose: "", purpose_source: "", purpose_kind: "", collapsed: false, package: true, top_files: [] },
  ],
  edges: [{ source: "web", target: "src", weight: 6, kinds: { imports: 6 }, examples: [] }],
  edges_total: 1,
  filtered: {},
  degraded_reason: "",
  context,
};

function focusPayload(level: string, id: string) {
  const subjectPath = level === "file" ? id : "src/payment.ts";
  const node = (nodeId: string, path: string, lane: string, status: string, extra: Record<string, unknown> = {}) => ({
    id: nodeId,
    label: path.split("/").pop(),
    kind: "file",
    module: path.split("/").slice(0, -1).join("/"),
    lane,
    path,
    line: 0,
    end_line: 0,
    status,
    exactness: "current_index",
    target_ids: [],
    boundaries: [],
    important_public_contract: false,
    ...extra,
  });
  return {
    level: level === "module" ? "architecture" : level,
    graph: {
      level: level === "module" ? "architecture" : level,
      focus: level === "module" ? null : `file:${subjectPath}`,
      nodes: [
        node(`file:${subjectPath}`, subjectPath, "", "subject", level === "file" ? { symbols: [{ ...charge, change: "", caller_count: -1, usage_count: -1, centrality_rank: null, centrality_percentile: null }] } : {}),
        node("file:web/app.ts", "web/app.ts", "used_by", "context"),
      ],
      edges: [
        { id: "e1", source: "file:web/app.ts", target: `file:${subjectPath}`, kind: "imports", exactness: "current_index", weight: 2, uncertain: false },
      ],
      truncated: false,
      stats: {},
    },
    subject: { kind: level, id, path: subjectPath, label: subjectPath.split("/").pop() },
    context,
  };
}

interface Recorded {
  urls: string[];
  posts: { url: string; body: Record<string, unknown> }[];
}

function mockApi(overrides: Record<string, (url: string, init?: RequestInit) => Response | undefined> = {}): Recorded {
  const recorded: Recorded = { urls: [], posts: [] };
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    recorded.urls.push(url);
    if (init?.method === "POST") recorded.posts.push({ url, body: JSON.parse(String(init.body || "{}")) });
    for (const [key, handler] of Object.entries(overrides)) {
      if (url.includes(key)) {
        const result = handler(url, init);
        if (result) return Promise.resolve(result);
      }
    }
    if (url.includes("/v1/code-map/projects")) {
      return Promise.resolve(json({ projects: [{ project_id: "proj_repo", root: "/repo", label: "repo", project: "repo", indexed: true, active: true }] }));
    }
    if (url.includes("/v1/code-map/files")) return Promise.resolve(json({ project: { root: "/repo", label: "repo" }, total_files: 2, files: [paymentFile, checkoutFile] }));
    if (url.includes("/v1/code-map/file-symbols")) {
      const path = new URL(url, "http://x").searchParams.get("path");
      return Promise.resolve(json({ path, total_symbols: path === "src/payment.ts" ? 1 : 0, symbols: path === "src/payment.ts" ? [charge] : [], truncated: false }));
    }
    if (url.includes("/v1/code-map/file-content")) {
      return Promise.resolve(new Response("export const payment = true;\nexport function chargeCard() {\n  return payment;\n}", { status: 200, headers: { "Content-Type": "text/plain" } }));
    }
    if (url.includes("/v1/code-map/file-changes")) return Promise.resolve(json({ path: "src/payment.ts", status: "M", added: [], modified: [[2, 3]], deleted: [] }));
    if (url.includes("/v1/code-map/relationships")) {
      const params = new URL(url, "http://x").searchParams;
      return Promise.resolve(
        json(
          params.get("symbol_id")
            ? relationships({ kind: "symbol", path: "src/payment.ts", symbol: "chargeCard" }, ["src/checkout.ts"], ["src/gateway.ts"])
            : relationships({ kind: "file", path: params.get("path") }, ["src/checkout.ts", "web/app.ts"], ["src/util.ts"]),
        ),
      );
    }
    if (url.includes("/v1/code-map/references")) return Promise.resolve(json({ symbol_id: "charge", reference_count: 1, references: [{ path: "src/checkout.ts", line: 8, column: 3, end_line: 8, snippet: "chargeCard(total)", caller: "checkout", edge_kind: "call", provenance: "ast" }], truncated: false }));
    if (url.includes("/v1/code-map/changes")) return Promise.resolve(json(changes));
    if (url.includes("/v1/code-map/modules")) return Promise.resolve(json(modules));
    if (url.includes("/v1/code-map/focus")) {
      const params = new URL(url, "http://x").searchParams;
      return Promise.resolve(json(focusPayload(params.get("level")!, params.get("id")!)));
    }
    if (url.includes("/v1/code-map/search")) return Promise.resolve(json({ query: "", results: [], text_results: [] }));
    if (url.includes("/v1/code-map/activity")) {
      return Promise.resolve(
        json({
          session_id: "s1",
          host: "claude",
          status: "running",
          handoff_supported: true,
          agent_run_supported: false,
          cursor: "2026-09-28T10:00:00+00:00",
          events: [{ id: "ev1", session_id: "s1", kind: "edit", at: new Date().toISOString(), label: "Edited payment.ts", path: "src/payment.ts", line: 2 }],
          sessions: [{ id: "claude:s1", host: "claude", session_id: "s1", status: "running", last_at: new Date().toISOString(), live: true }],
          selected_session: "claude:s1",
        }),
      );
    }
    if (url.includes("/v1/code-map/editor")) return Promise.resolve(json({ available: false, preferred: null, editors: [] }));
    if (url.includes("/v1/code-map/review")) {
      return Promise.resolve(json({ review_id: "r-1", short_id: "r-1", review_path: "/r/abc", review_url: "", revision_id: "rr-1", revision_number: 1, revision_created: true }));
    }
    if (url.includes("/v1/code-map/ask/options")) return Promise.resolve(json({ driver: "configured", model: "" }));
    if (url.includes("/v1/code-map/ask/stream")) {
      const answer = { body: "The retry is bounded by the current code.", references: [{ path: "src/payment.ts", start_line: 2, end_line: 3 }] };
      return Promise.resolve(new Response(`${JSON.stringify({ type: "accepted", driver: "configured", model: "" })}\n${JSON.stringify({ type: "delta", text: answer.body })}\n${JSON.stringify({ type: "result", result: answer })}\n`, { status: 200, headers: { "Content-Type": "application/x-ndjson" } }));
    }
    return Promise.resolve(json({}));
  });
  return recorded;
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <CodeMap />
      <HistoryProbe />
    </MemoryRouter>,
  );
}

describe("CodeMap", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.removeItem("lemoncrow.preferredEditor");
    localStorage.removeItem("lemoncrow.code.ask:proj_repo");
    localStorage.removeItem("lemoncrow.code.ask.driver");
    localStorage.removeItem("lemoncrow.code.ask.model");
  });

  it("paints Source from the file list alone and shows Used by / Uses from the shared provider", async () => {
    const recorded = mockApi();
    const user = userEvent.setup();
    renderAt("/code");

    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    await user.click(await within(explorer).findByRole("button", { name: /payment\.ts/ }));

    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    await within(inspector).findByText("checkout.ts", { exact: false });
    expect(within(inspector).getByText("Used by · 2")).toBeInTheDocument();
    expect(within(inspector).getByText("Uses · 1")).toBeInTheDocument();
    expect(within(inspector).getByText("Current index")).toBeInTheDocument();
    expect(within(inspector).getByText(/Left out 1 name match across languages/)).toBeInTheDocument();
    expect(await screen.findByTestId("highlighted-source")).toHaveTextContent("chargeCard");
    // No repository-scale graph is requested anywhere.
    expect(recorded.urls.some((url) => url.includes("/v1/code-map/full"))).toBe(false);
    expect(recorded.urls.some((url) => url.includes("relationships?path=src%2Fpayment.ts"))).toBe(true);
    // Uncommitted edits: explorer mark, header badge, gutter ranges.
    expect(within(explorer).getAllByText("M").length).toBeGreaterThan(0);
    expect(await screen.findByText(/Modified · \+4 −1/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("highlighted-source").dataset.changed).toContain("[[2,3]]"));
  });

  it("shows one empty state with the restart command when the code index does not answer", async () => {
    mockApi({ "/v1/code-map/projects": () => new Response("down", { status: 503 }) });
    renderAt("/code");

    expect(await screen.findByText("Code index unavailable")).toBeInTheDocument();
    // The command is code, not prose with literal backticks.
    const command = screen.getByText("bash ~/.lemoncrow/install/scripts/local_server.sh restart");
    expect(command.tagName).toBe("CODE");
    expect(screen.queryByText(/`/)).not.toBeInTheDocument();
    // No empty explorer, inspector or live chip beside it.
    expect(screen.queryByRole("complementary", { name: "Explorer" })).not.toBeInTheDocument();
    expect(screen.queryByText("No matching files")).not.toBeInTheDocument();
    expect(screen.queryByText("Open a file or symbol")).not.toBeInTheDocument();
    // No project, so no Changes menu that would read "Reading the worktree…" forever.
    expect(screen.queryByRole("button", { name: /Changes/ })).not.toBeInTheDocument();
  });

  it("labels the live-session count and, without a live workspace, points Sessions to Activity", async () => {
    mockApi();
    render(
      <MemoryRouter initialEntries={["/code"]}>
        <ProductCapabilitiesProvider value={{
          ...LOCAL_DASHBOARD_PRODUCT_CAPABILITIES,
          features: { ...LOCAL_DASHBOARD_PRODUCT_CAPABILITIES.features, "runtime.live_workspace": false },
        }}>
          <CodeMap />
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );
    const sessions = await screen.findByRole("region", { name: "Sessions" });
    expect(within(sessions).getByRole("link", { name: "Activity" })).toHaveAttribute("href", "/activity");
    expect(sessions).not.toHaveTextContent("not available here");
    expect(screen.getByRole("button", { name: /^Live activity:/ })).toHaveTextContent(/^\d+ live$/);
  });

  it("reads a Review-linked workspace absent from the indexed project picker", async () => {
    const root = "/tmp/lemoncrow-review-demo";
    const recorded = mockApi({
      "/v1/code-map/projects": () => json({ projects: [] }),
      "/v1/code-map/files": () => json({ project: { root, label: "lemoncrow-review-demo" }, total_files: 1, files: [paymentFile] }),
    });
    renderAt(`/code?repo=${encodeURIComponent(root)}&file=src/payment.ts&view=source`);

    await waitFor(() => expect(screen.getByTestId("highlighted-source")).toHaveTextContent("chargeCard"));
    expect(recorded.urls.some((url) => url.includes("/v1/code-map/file-content") && new URL(url, "http://x").searchParams.get("project_root") === root)).toBe(true);
    expect(screen.queryByText("Source is unavailable.")).not.toBeInTheDocument();
  });

  it("opens a deep-linked symbol in the full file with symbol relationships and references", async () => {
    const recorded = mockApi();
    renderAt("/code?repo=proj_repo&file=src/payment.ts&symbol=charge&view=source");

    const inspector = await screen.findByRole("complementary", { name: "Inspector" });
    await within(inspector).findByText("PaymentGateway.chargeCard");
    await within(inspector).findByText("gateway.ts", { exact: false });
    await within(inspector).findByText("src/checkout.ts");
    const source = await screen.findByTestId("highlighted-source");
    await waitFor(() => expect(source.querySelector('[data-code-line="2"]')).toHaveClass("border-cyan-400"));
    expect(recorded.urls.some((url) => url.includes("relationships?symbol_id=charge"))).toBe(true);
  });

  it("opens a file that is not in the listing and keeps navigation in browser history", async () => {
    mockApi();
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo&file=generated/out.ts&view=source");

    expect(await screen.findByText("out.ts", { selector: "span" })).toBeInTheDocument();
    const explorer = screen.getByRole("complementary", { name: "Explorer" });
    await user.click(await within(explorer).findByRole("button", { name: /checkout\.ts/ }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("file=src%2Fcheckout.ts"));
    await user.click(screen.getByRole("button", { name: "Browser back" }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent(/file=generated(%2F|\/)out\.ts/));
  });

  it("groups uncommitted work by session and reviews one session's files", async () => {
    const recorded = mockApi();
    const user = userEvent.setup();
    renderAt("/code");

    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    const sessions = await within(explorer).findByRole("region", { name: "Sessions" });
    await user.click(within(sessions).getByRole("button", { name: /Tighten retries/ }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("session=claude%3As1"));
    expect(within(explorer).queryByRole("button", { name: /checkout\.ts/ })).not.toBeInTheDocument();
    expect(within(explorer).getByRole("button", { name: /payment\.ts/ })).toBeInTheDocument();
    expect(within(sessions).getByRole("button", { name: /Unattributed/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Changes/ }));
    const menu = screen.getByRole("dialog", { name: "Uncommitted changes" });
    // Sessions read by title and host name, never the raw session id.
    expect(within(menu).getByRole("button", { name: "Tighten retries" })).toHaveAttribute("title", "Tighten retries");
    expect(within(menu).getByText("Claude Code")).toBeInTheDocument();
    expect(within(menu).queryByText(/author only/)).not.toBeInTheDocument();
    await user.click(within(menu).getAllByRole("button", { name: "Review" })[0]);
    await waitFor(() => expect(screen.getByTestId("location-path")).toHaveTextContent("/r/abc"));
    // The Review's Back returns to this exact Code location.
    const back = new URLSearchParams(screen.getByTestId("location-search").textContent ?? "").get("from") ?? "";
    expect(back.startsWith("/code?")).toBe(true);
    expect(new URLSearchParams(back.slice("/code?".length)).get("session")).toBe("claude:s1");
    const review = recorded.posts.find((post) => post.url.includes("/v1/code-map/review"))!;
    expect(review.body).toEqual({ project_root: "proj_repo", session: "claude:s1" });
  });

  it("opens the module overview, focuses a module and a file, and returns by the breadcrumb", async () => {
    const recorded = mockApi();
    const user = userEvent.setup();
    renderAt("/code?view=map");

    const map = await screen.findByRole("group", { name: "Module map" });
    const srcCard = await within(map).findByRole("button", { name: /^src, 30 files/ });
    expect(within(map).getByText("Payments core.")).toBeInTheDocument();
    await user.click(srcCard);
    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    expect(await within(inspector).findByText("Used by · 1")).toBeInTheDocument();

    fireEvent.doubleClick(srcCard);
    await screen.findByRole("group", { name: "Code map" });
    expect(recorded.urls.some((url) => url.includes("/v1/code-map/focus?level=module&id=src"))).toBe(true);
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("map=module%3Asrc"));

    const location = screen.getByRole("navigation", { name: "Map location" });
    await user.click(within(location).getByRole("button", { name: "repo" }));
    await screen.findByRole("group", { name: "Module map" });
  });

  it("offers Source, Map and 3D, and sends an old 2D link to the Map", async () => {
    mockApi();
    renderAt("/code?view=2d");

    await screen.findByRole("group", { name: "Module map" });
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("view=map"));
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("view=2d");
    expect(screen.getByRole("button", { name: "Map" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Source" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "3D" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "2D" })).not.toBeInTheDocument();
  });

  it("describes a module on request with the configured model and says so", async () => {
    const recorded = mockApi({
      "/v1/code-map/modules": () => json({ ...modules, purpose_generation: { available: true, model: "gpt-test" } }),
      "/v1/code-map/module-purpose": () =>
        json({
          module: "web",
          purpose: "Browser checkout pages.",
          purpose_kind: "generated",
          purpose_source: "gpt-test",
          purpose_at: new Date().toISOString(),
          purpose_evidence: ["web/app.ts", "web/cart.ts"],
        }),
    });
    const user = userEvent.setup();
    renderAt("/code?view=map");

    const map = await screen.findByRole("group", { name: "Module map" });
    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    // A module's own statement is never replaced by a generated one.
    await user.click(await within(map).findByRole("button", { name: /^src, 30 files/ }));
    expect(await within(inspector).findByText("from src/README.md")).toBeInTheDocument();
    expect(within(inspector).queryByRole("button", { name: /Describe/ })).toBeNull();

    await user.click(within(map).getByRole("button", { name: /^web, 10 files/ }));
    await user.click(await within(inspector).findByRole("button", { name: "Describe with gpt-test" }));
    expect(await within(inspector).findByText("Browser checkout pages.")).toBeInTheDocument();
    expect(within(inspector).getByText(/^Generated by gpt-test from 2 files/)).toHaveAttribute("title", "web/app.ts\nweb/cart.ts");
    expect(within(inspector).getByRole("button", { name: "Describe again" })).toBeInTheDocument();
    expect(within(map).getByText("Browser checkout pages.")).toHaveAttribute("title", "Generated by a model on request: Browser checkout pages.");
    expect(recorded.posts.find((post) => post.url.includes("/v1/code-map/module-purpose"))!.body).toMatchObject({ module: "web" });
  });

  it("lists files agents re-edited this week and states the count on the file", async () => {
    const hoursAgo = (hours: number) => new Date(Date.now() - hours * 3_600_000).toISOString();
    mockApi({
      "/v1/code-map/hotspots": () =>
        json({
          project: { root: "/repo", label: "repo" },
          days: 7,
          since: hoursAgo(168),
          files: [
            { path: "gone.ts", edits: 9, sessions: ["claude:s1"], last_at: hoursAgo(1) },
            { path: "src/checkout.ts", edits: 5, sessions: ["claude:s1", "codex:s2"], last_at: hoursAgo(2) },
          ],
          reedited_total: 2,
          edited_total: 3,
          sessions: 2,
          ledgers_scanned: 2,
          truncated: false,
          rule: "",
        }),
    });
    const user = userEvent.setup();
    renderAt("/code");

    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    const hot = await within(explorer).findByRole("region", { name: "Re-edited this week" });
    // Only files still in the project are listed.
    expect(within(hot).getAllByRole("button", { name: /edits by/ })).toHaveLength(1);
    await user.click(within(hot).getByRole("button", { name: "src/checkout.ts: 5 edits by 2 sessions" }));
    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    expect(await within(inspector).findByText(/Agents edited this file 5 times this week, in 2 sessions; the last 2h ago\./)).toBeInTheDocument();
  });

  it("keeps all explorer section headers visible and folds each section independently", async () => {
    mockApi();
    const user = userEvent.setup();
    renderAt("/code");

    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    const files = within(explorer).getByRole("region", { name: "Project files" });
    const sessions = within(explorer).getByRole("region", { name: "Sessions" });
    const hotspots = within(explorer).getByRole("region", { name: "Re-edited this week" });
    const filesToggle = within(files).getByRole("button", { name: /Project files/ });
    const sessionsToggle = within(sessions).getByRole("button", { name: /^Sessions/ });
    const hotspotsToggle = within(hotspots).getByRole("button", { name: /Re-edited this week/ });

    expect(within(hotspots).getByText("No re-edited files.")).toBeInTheDocument();
    expect(within(files).getByRole("button", { name: /payment\.ts/ })).toBeInTheDocument();
    await user.click(filesToggle);
    expect(filesToggle).toHaveAttribute("aria-expanded", "false");
    expect(within(files).queryByRole("button", { name: /payment\.ts/ })).toBeNull();
    expect(within(sessions).getByText("Tighten retries")).toBeInTheDocument();

    await user.click(sessionsToggle);
    await user.click(hotspotsToggle);
    expect(sessionsToggle).toHaveAttribute("aria-expanded", "false");
    expect(hotspotsToggle).toHaveAttribute("aria-expanded", "false");
    expect(within(explorer).getByRole("button", { name: /Project files/ })).toBeVisible();
    expect(within(explorer).getByRole("button", { name: /Sessions/ })).toBeVisible();
    expect(within(explorer).getByRole("button", { name: /Re-edited this week/ })).toBeVisible();

    await user.click(filesToggle);
    expect(filesToggle).toHaveAttribute("aria-expanded", "true");
    expect(within(files).getByRole("button", { name: /payment\.ts/ })).toBeInTheDocument();
  });

  it("resizes lower explorer sections while reserving room for project files", async () => {
    mockApi();
    renderAt("/code");
    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    const sessions = within(explorer).getByRole("region", { name: "Sessions" });
    const hotspots = within(explorer).getByRole("region", { name: "Re-edited this week" });
    const toolbar = within(explorer).getByRole("searchbox", { name: "Filter files" }).closest(".border-b") as HTMLElement;
    const bounds = (height: number) => ({ height }) as DOMRect;
    vi.spyOn(explorer, "getBoundingClientRect").mockReturnValue(bounds(1000));
    vi.spyOn(toolbar, "getBoundingClientRect").mockReturnValue(bounds(50));
    vi.spyOn(sessions, "getBoundingClientRect").mockReturnValue(bounds(220));
    vi.spyOn(hotspots, "getBoundingClientRect").mockReturnValue(bounds(220));

    fireEvent.keyDown(within(sessions).getByRole("button", { name: "Resize Sessions" }), { key: "ArrowUp" });
    expect(sessions).toHaveStyle({ height: "248px" });

    const handle = within(hotspots).getByRole("button", { name: "Resize Re-edited files" });
    handle.setPointerCapture = vi.fn();
    handle.hasPointerCapture = vi.fn(() => true);
    handle.releasePointerCapture = vi.fn();
    const pointer = (type: string, clientY: number) => {
      const event = new MouseEvent(type, { bubbles: true, button: 0, clientY });
      Object.defineProperty(event, "pointerId", { value: 1 });
      fireEvent(handle, event);
    };
    pointer("pointerdown", 300);
    pointer("pointermove", 240);
    expect(hotspots).toHaveStyle({ height: "280px" });
    pointer("pointerup", 240);
    expect(hotspots).toHaveStyle({ height: "280px" });
    pointer("pointerdown", 300);
    pointer("pointermove", -1000);
    expect(hotspots).toHaveStyle({ height: "570px" });
    pointer("pointerup", -1000);
  });

  it("opens the selected file from the Map toggle and opens its symbols in the symbol focus", async () => {
    const recorded = mockApi();
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo&file=src/payment.ts&view=source");

    expect(screen.queryByRole("button", { name: /Map this/ })).not.toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Map" }));
    const map = await screen.findByRole("group", { name: "Code map" });
    expect(recorded.urls.some((url) => url.includes("focus?level=file&id=src%2Fpayment.ts"))).toBe(true);
    await user.click(await within(map).findByRole("button", { name: "Open symbol chargeCard" }));
    await waitFor(() => expect(recorded.urls.some((url) => url.includes("focus?level=symbol&id=charge"))).toBe(true));
    const location = screen.getByRole("navigation", { name: "Map location" });
    expect(within(location).getByRole("button", { name: "payment.ts" })).toBeInTheDocument();
  });

  it("opens the shared chat from Code and answers with file context", async () => {
    const recorded = mockApi();
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo&file=src/payment.ts&view=source");

    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    expect(await within(explorer).findByRole("img", { name: "Agent edit now" })).toBeInTheDocument();

    // Ask lives once, in the toolbar; the inspector has no second Ask button.
    const inspector = screen.getByRole("complementary", { name: "Inspector" });
    expect(within(inspector).queryByRole("button", { name: /^Ask about/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Ask AI about code" }));
    const chat = await screen.findByRole("region", { name: "Code chat" });
    expect(within(chat).getByLabelText("Code Ask focus")).toHaveTextContent("Focused file");
    expect(within(chat).getByLabelText("Code Ask focus")).toHaveTextContent("src/payment.ts");
    expect(within(chat).getByLabelText("Code Ask focus")).toHaveTextContent("Answers can reference related project files.");
    fireEvent.change(within(chat).getByLabelText("External file picker"), { target: { files: [new File(["retry budget is two"], "notes.txt", { type: "text/plain" })] } });
    await within(chat).findByText(/notes\.txt/);
    await user.type(within(chat).getByRole("textbox", { name: "Ask Code" }), "Why retry twice?");
    await user.click(within(chat).getByRole("button", { name: "Send Ask question" }));
    expect(await within(chat).findByText("The retry is bounded by the current code.")).toBeInTheDocument();
    const request = recorded.posts.find((post) => post.url.includes("/code-map/ask/stream"))!;
    expect(request.body).toMatchObject({
      project_root: "proj_repo", path: "src/payment.ts", question: "Why retry twice?",
      attachments: [{ name: "notes.txt", content: "retry budget is two" }],
    });
    expect(recorded.posts.some((post) => post.url.includes("agent-handoff"))).toBe(false);

    await user.click(within(chat).getByRole("button", { name: "Close Ask" }));
    await user.click(screen.getByRole("button", { name: "Ask AI about code" }));
    const reopened = await screen.findByRole("region", { name: "Code chat" });
    expect(await within(reopened).findByText("The retry is bounded by the current code.")).toBeInTheDocument();
    await user.type(within(reopened).getByRole("textbox", { name: "Ask Code" }), "And the next attempt?");
    await user.click(within(reopened).getByRole("button", { name: "Send Ask question" }));
    await waitFor(() => expect(recorded.posts.filter((post) => post.url.includes("/code-map/ask/stream"))).toHaveLength(2));
    expect(recorded.posts.filter((post) => post.url.includes("/code-map/ask/stream"))[1].body.history).toEqual([
      { role: "user", body: "Why retry twice?" },
      { role: "assistant", body: "The retry is bounded by the current code." },
    ]);
  });

  it("passes selected source text and lines into the same Code chat", async () => {
    const recorded = mockApi();
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo&file=src/payment.ts&view=source");

    await user.click(await screen.findByRole("button", { name: "Select code" }));
    await user.click(screen.getByRole("button", { name: "Ask selection" }));
    const chat = await screen.findByRole("region", { name: "Code chat" });
    expect(within(chat).getByLabelText("Code Ask focus")).toHaveTextContent("Selected code");
    expect(within(chat).getByLabelText("Code Ask focus")).toHaveTextContent("src/payment.ts · lines 2–3");
    await user.type(within(chat).getByRole("textbox", { name: "Ask Code" }), "Explain this selection");
    await user.click(within(chat).getByRole("button", { name: "Send Ask question" }));
    await waitFor(() => expect(recorded.posts.some((post) => post.url.includes("/code-map/ask/stream"))).toBe(true));
    expect(recorded.posts.find((post) => post.url.includes("/code-map/ask/stream"))!.body).toMatchObject({
      path: "src/payment.ts", line: 2, end_line: 3, selected_text: "export function chargeCard() {",
    });
  });

  it("returns to the Review it was opened from", async () => {
    mockApi();
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo&file=src/payment.ts&view=source&from=%2Fr%2Fabc%3Fmap%3D1");

    await user.click(await screen.findByRole("button", { name: /Review/, description: undefined }));
    await waitFor(() => expect(screen.getByTestId("location-path")).toHaveTextContent("/r/abc"));
  });

  it("ignores a Back part that would leave the app", async () => {
    mockApi();
    renderAt("/code?repo=proj_repo&file=src/payment.ts&view=source&from=%2F%2Fevil.example%2Fr%2Fabc");
    await screen.findByRole("complementary", { name: "Explorer" });
    expect(screen.queryByTitle("Back to where you came from")).toBeNull();
    expect(screen.queryByTitle("Back to the Review location you came from")).toBeNull();
  });

  it("opens Code Ask with an activity line and restores the inspector after closing", async () => {
    localStorage.setItem("lemoncrow.preferredEditor", "cursor");
    const recorded = mockApi({
      "/v1/code-map/editor": (_url, init) =>
        init?.method === "POST"
          ? json({ opened: true, editor: "cursor", label: "Cursor", line: 2, column: 1, pid: 42 })
          : json({ available: true, preferred: { id: "vscode", label: "VS Code" }, editors: [{ id: "vscode", label: "VS Code" }, { id: "cursor", label: "Cursor" }] }),
    });
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo&file=src/payment.ts&view=source");

    const inspector = await screen.findByRole("complementary", { name: "Inspector" });
    await user.click(await within(inspector).findByRole("button", { name: /Edited payment\.ts · L2/ }));
    const line = await waitFor(() => {
      const element = screen.getByLabelText("Source viewer").querySelector('[data-code-line="2"]');
      expect(element).toHaveClass("border-cyan-400", "bg-cyan-400/10");
      return element;
    });
    expect(line).toBeTruthy();
    expect(screen.getByRole("button", { name: "Line" })).toHaveAttribute("title", "L2");
    expect(screen.getByTestId("location-search")).toHaveTextContent("line=2");

    await user.click(screen.getByRole("button", { name: "Open in editor" }));
    await waitFor(() => expect(recorded.posts.some((post) => post.url.includes("/v1/code-map/editor"))).toBe(true));
    expect(recorded.posts.find((post) => post.url.includes("/v1/code-map/editor"))!.body).toEqual({
      project_root: "proj_repo",
      path: "src/payment.ts",
      line: 2,
      column: 1,
      editor: "cursor",
    });

    await user.click(screen.getByRole("button", { name: "Ask AI about code" }));
    const chat = await screen.findByRole("region", { name: "Code chat" });
    expect(within(chat).getByText(/src\/payment.ts · PaymentGateway.chargeCard · line 2/)).toBeInTheDocument();
    await user.type(within(chat).getByRole("textbox", { name: "Ask Code" }), "Check the retry here.");
    await user.click(within(chat).getByRole("button", { name: "Send Ask question" }));
    await waitFor(() => expect(recorded.posts.some((post) => post.url.includes("/code-map/ask/stream"))).toBe(true));
    expect(recorded.posts.find((post) => post.url.includes("/code-map/ask/stream"))!.body).toMatchObject({
      project_root: "proj_repo", path: "src/payment.ts", line: 2, end_line: 2, symbol: "PaymentGateway.chargeCard",
    });
    await user.keyboard("{Escape}");
    expect(screen.getByRole("complementary", { name: "Inspector" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask AI about code" })).toHaveFocus());

    const explorer = screen.getByRole("complementary", { name: "Explorer" });
    await user.click(within(explorer).getByRole("button", { name: /checkout\.ts/ }));
    expect(await within(screen.getByRole("complementary", { name: "Inspector" })).findByText("src/checkout.ts")).toBeInTheDocument();
  });

  it("opens symbol and text search results at their exact location", async () => {
    const recorded = mockApi({
      "/v1/code-map/search": () =>
        json({
          query: "charge",
          results: [charge],
          text_results: [
            { id: "text::src/checkout.ts:3:5", path: "src/checkout.ts", line: 3, column: 5, text: "return chargeCard(total);", language: "TypeScript", file_type: "source" },
          ],
        }),
    });
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo");

    await user.type(await screen.findByRole("searchbox", { name: "Find code" }), "charge");
    await user.click(await screen.findByRole("button", { name: /chargeCard src\/payment\.ts/ }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("symbol=charge"));
    expect(recorded.urls.some((url) => url.includes("relationships?symbol_id=charge"))).toBe(true);
    await waitFor(() =>
      expect(screen.getByLabelText("Source viewer").querySelector('[data-code-line="2"]')).toHaveClass("border-cyan-400"),
    );

    await user.type(screen.getByRole("searchbox", { name: "Find code" }), "charge");
    await user.click(await screen.findByRole("button", { name: /return chargeCard\(total\); src\/checkout\.ts/ }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("file=src%2Fcheckout.ts"));
    expect(screen.getByTestId("location-search")).toHaveTextContent("line=3");
    await waitFor(() =>
      expect(screen.getByLabelText("Source viewer").querySelector('[data-code-line="3"]')).toHaveClass("border-cyan-400"),
    );
    expect(screen.getByRole("button", { name: "Line" })).toHaveAttribute("title", "L3");
  });

  it("switches between indexed worktrees from the header", async () => {
    const recorded = mockApi({
      "/v1/code-map/projects": () => json({ projects: [
        { project_id: "proj_repo", root: "/repo", label: "repo", project: "repo", worktree: "main", branch: "main", indexed: true, active: true },
        { project_id: "proj_other", root: "/other", label: "feature", project: "repo", worktree: "feature", branch: "feature/payments", indexed: true, active: false },
      ] }),
    });
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo");
    await screen.findByRole("complementary", { name: "Explorer" });
    await user.click(screen.getByRole("combobox", { name: "Project and worktree" }));
    const projectSearch = screen.getByRole("searchbox", { name: "Search projects" });
    await user.type(projectSearch, "payments");
    expect(screen.queryByRole("option", { name: /repo \/ main · main/ })).toBeNull();
    await user.click(screen.getByRole("option", { name: /repo \/ feature · feature\/payments/ }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("repo=proj_other"));
    await waitFor(() => expect(recorded.urls.some((url) => url.includes("/v1/code-map/files?project_root=proj_other"))).toBe(true));
  });

  it("filters changed files, ignored files, and project folders", async () => {
    const plain = { ...paymentFile, id: "file::plain", label: "plain.ts", qualified_name: "src/plain.ts", path: "src/plain.ts" };
    const data = { ...paymentFile, id: "file::schema", label: "schema.sql", qualified_name: "data/schema.sql", path: "data/schema.sql", kind: "data", file_type: "data" };
    const ignored = { ...paymentFile, id: "file::ignored", label: "build.log", qualified_name: "cache/build.log", path: "cache/build.log", ignored: true };
    const recorded = mockApi({
      "/v1/code-map/files": (url) => json({ project: { root: "/repo", label: "repo" }, total_files: 4, files: [paymentFile, checkoutFile, plain, data, ...(url.includes("include_ignored=true") ? [ignored] : [])] }),
    });
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo");
    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    expect(await within(explorer).findByRole("button", { name: /plain\.ts/ })).toBeInTheDocument();
    await user.click(within(explorer).getByRole("button", { name: "Changed since HEAD" }));
    expect(within(explorer).queryByRole("button", { name: /plain\.ts/ })).toBeNull();
    expect(within(explorer).getByRole("button", { name: /payment\.ts/ })).toBeInTheDocument();
    await user.click(within(explorer).getByRole("button", { name: "Changed since HEAD" }));
    await user.click(within(explorer).getByRole("button", { name: "Explorer filters" }));
    await user.click(screen.getByRole("menuitemcheckbox", { name: "Show Git ignored files" }));
    expect(await within(explorer).findByRole("button", { name: /build\.log.*ignored/ })).toBeInTheDocument();
    expect(recorded.urls.some((url) => url.includes("include_ignored=true"))).toBe(true);
    await user.click(within(explorer).getByRole("button", { name: "Explorer filters" }));
    await user.click(screen.getByRole("menuitemradio", { name: "src" }));
    expect(within(explorer).queryByRole("button", { name: /schema\.sql/ })).toBeNull();
    expect(within(explorer).getByRole("button", { name: /plain\.ts/ })).toBeInTheDocument();
    await user.click(within(explorer).getByRole("button", { name: "Explorer filters" }));
    await user.click(screen.getByRole("menuitemradio", { name: "All folders" }));
    await user.click(within(explorer).getByRole("button", { name: "Explorer filters" }));
    await user.click(screen.getByRole("menuitemradio", { name: "Data" }));
    expect(within(explorer).getByRole("button", { name: /schema\.sql/ })).toBeInTheDocument();
    expect(within(explorer).queryByRole("button", { name: /plain\.ts/ })).toBeNull();
  });

  it("uses the active indexed project and reuses cached source when revisiting a file", async () => {
    const recorded = mockApi({
      "/v1/code-map/projects": () => json({ projects: [{ root: "/repo", label: "repo", indexed: true, active: true }] }),
    });
    const user = userEvent.setup();
    renderAt("/code");

    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    expect(recorded.urls.some((url) => url.includes("/v1/code-map/files?project_root=%2Frepo"))).toBe(true);
    await user.click(await within(explorer).findByRole("button", { name: /payment\.ts/ }));
    expect(await screen.findByTestId("highlighted-source")).toHaveTextContent("export const payment = true;");
    await user.click(within(explorer).getByRole("button", { name: /checkout\.ts/ }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("file=src%2Fcheckout.ts"));
    await user.click(within(explorer).getByRole("button", { name: /payment\.ts/ }));
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("file=src%2Fpayment.ts"));
    const contentFetches = recorded.urls.filter((url) => url.includes("file-content?path=src%2Fpayment.ts"));
    const symbolFetches = recorded.urls.filter((url) => url.includes("file-symbols?path=src%2Fpayment.ts"));
    expect(contentFetches).toHaveLength(1);
    expect(symbolFetches).toHaveLength(1);
  });

  it("reviews all uncommitted work without a scope", async () => {
    const recorded = mockApi();
    const user = userEvent.setup();
    renderAt("/code?repo=proj_repo");

    await user.click(await screen.findByRole("button", { name: /Changes/ }));
    const menu = screen.getByRole("dialog", { name: "Uncommitted changes" });
    expect(within(menu).getByText("Tighten retries")).toBeInTheDocument();
    expect(within(menu).getByText("Unattributed")).toBeInTheDocument();
    await user.click(within(menu).getByRole("button", { name: "Review all 2" }));
    await waitFor(() => expect(screen.getByTestId("location-path")).toHaveTextContent("/r/abc"));
    expect(recorded.posts.find((post) => post.url.includes("/v1/code-map/review"))!.body).toEqual({ project_root: "proj_repo" });
  });
});

// Below Tailwind's xl (1280px) the explorer is the toolbar's Files sheet, not a pane in its own scroll box above the editor.
describe("CodeMap below the xl width", () => {
  /** A window `wide` or not; the returned function resizes it and tells the page. */
  function windowWidth(wide: boolean): (next: boolean) => void {
    const listeners = new Set<() => void>();
    const state = { wide };
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: (query: string) => ({
        get matches() {
          return state.wide && query === "(min-width: 1280px)";
        },
        media: query,
        addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
        removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
      }),
    });
    return (next) => {
      state.wide = next;
      act(() => listeners.forEach((listener) => listener()));
    };
  }

  afterEach(() => {
    vi.restoreAllMocks();
    delete (window as { matchMedia?: unknown }).matchMedia;
  });

  it("folds the explorer into a Files sheet; choosing a file closes the sheet and opens the file", async () => {
    windowWidth(false);
    mockApi();
    const user = userEvent.setup();
    renderAt("/code");

    const files = await screen.findByRole("button", { name: "Files" });
    expect(files).toHaveAttribute("aria-haspopup", "dialog");
    expect(files).toHaveAttribute("aria-expanded", "false");
    // No tree on the page: the open view and the inspector are the only panes under the toolbar.
    expect(screen.queryByRole("complementary", { name: "Explorer" })).not.toBeInTheDocument();
    expect(screen.getByRole("complementary", { name: "Inspector" })).toBeInTheDocument();

    await user.click(files);
    const sheet = await screen.findByRole("dialog", { name: "Files" });
    expect(files).toHaveAttribute("aria-expanded", "true");
    expect(sheet.contains(document.activeElement)).toBe(true);
    const explorer = within(sheet).getByRole("complementary", { name: "Explorer" });
    expect(within(explorer).queryByLabelText("Resize code explorer")).not.toBeInTheDocument();

    await user.click(within(explorer).getByRole("button", { name: /payment\.ts/ }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Files" })).not.toBeInTheDocument());
    expect(screen.getByTestId("location-search")).toHaveTextContent("file=src%2Fpayment.ts");
    expect(await screen.findByTestId("highlighted-source")).toHaveTextContent("chargeCard");
    expect(files).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps focus inside the Files sheet and closes it on Esc, back on the Files button", async () => {
    windowWidth(false);
    mockApi();
    const user = userEvent.setup();
    renderAt("/code");

    const files = await screen.findByRole("button", { name: "Files" });
    await user.click(files);
    const sheet = await screen.findByRole("dialog", { name: "Files" });
    for (let step = 0; step < 15; step += 1) {
      await user.tab();
      expect(sheet.contains(document.activeElement)).toBe(true);
    }
    for (let step = 0; step < 3; step += 1) {
      await user.tab({ shift: true });
      expect(sheet.contains(document.activeElement)).toBe(true);
    }

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Files" })).not.toBeInTheDocument());
    expect(files).toHaveFocus();
    expect(screen.getByTestId("location-search")).toHaveTextContent("");
  });

  it("keeps the explorer's filter when the Files sheet closes and opens again", async () => {
    windowWidth(false);
    mockApi();
    const user = userEvent.setup();
    renderAt("/code");

    const files = await screen.findByRole("button", { name: "Files" });
    await user.click(files);
    let sheet = await screen.findByRole("dialog", { name: "Files" });
    await user.type(within(sheet).getByPlaceholderText("Filter paths…"), "pay");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Files" })).not.toBeInTheDocument());

    await user.click(files);
    sheet = await screen.findByRole("dialog", { name: "Files" });
    expect(within(sheet).getByPlaceholderText("Filter paths…")).toHaveValue("pay");
    expect(within(sheet).getByRole("button", { name: /payment\.ts/ })).toBeInTheDocument();
  });

  it("keeps the explorer as a resizable pane with no Files button from xl, and swaps on resize", async () => {
    const resize = windowWidth(true);
    mockApi();
    const user = userEvent.setup();
    renderAt("/code");

    const explorer = await screen.findByRole("complementary", { name: "Explorer" });
    expect(within(explorer).getByLabelText("Resize code explorer")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Files" })).not.toBeInTheDocument();

    resize(false);
    expect(screen.queryByRole("complementary", { name: "Explorer" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Files" }));
    expect(await screen.findByRole("dialog", { name: "Files" })).toBeInTheDocument();

    // Widening the window with the sheet open puts the pane back and drops the sheet.
    resize(true);
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Files" })).not.toBeInTheDocument());
    expect(screen.getByRole("complementary", { name: "Explorer" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Files" })).not.toBeInTheDocument();
  });
});

// A local Dashboard serves the deterministic Runtime Lite map plus actions that
// are genuinely local-machine capabilities: editor handoff, Code Ask and Review
// capture. Precise references and generated module purpose still need full Code.
describe("CodeMap on a local Dashboard", () => {
  const liteContext = {
    source: "current_index",
    backend: "runtime_lite",
    exactness: "current_index",
    fallback: false,
    relationship_basis: "imports",
    degraded: true,
    degraded_reason: "lite_import_level",
    readiness: { backend: "runtime_lite", graph: { state: "degraded", reason: "lite_import_level" } },
  };
  const importNote = "Import-level relationships · precise references need LemonCrow Hosted";

  function mockLiteApi(extra: Record<string, (url: string) => Response | undefined> = {}): Recorded {
    return mockApi({
      ...extra,
      "/v1/code-map/relationships": (url) => {
        const params = new URL(url, "http://x").searchParams;
        const subject = params.get("symbol_id")
          ? { kind: "symbol", path: "src/payment.ts", symbol: "chargeCard" }
          : { kind: "file", path: params.get("path") };
        return json({ ...relationships(subject, ["src/checkout.ts"], ["src/util.ts"]), context: liteContext });
      },
    });
  }

  function renderLocal(path: string) {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <ProductCapabilitiesProvider value={LOCAL_DASHBOARD_PRODUCT_CAPABILITIES}>
          <CodeMap />
          <HistoryProbe />
        </ProductCapabilitiesProvider>
      </MemoryRouter>,
    );
  }

  afterEach(() => vi.restoreAllMocks());

  it("keeps local editor, Ask and Review capture while hiding full-index references", async () => {
    const recorded = mockLiteApi();
    const user = userEvent.setup();
    renderLocal("/code?repo=proj_repo&file=src/payment.ts&symbol=charge&view=source");

    const inspector = await screen.findByRole("complementary", { name: "Inspector" });
    await within(inspector).findByText("PaymentGateway.chargeCard");
    expect(await within(inspector).findByText(importNote)).toBeInTheDocument();
    await within(inspector).findByText("checkout.ts", { exact: false });
    expect(await screen.findByTestId("highlighted-source")).toHaveTextContent("chargeCard");

    expect(within(inspector).queryByRole("button", { name: /^Ask about/ })).not.toBeInTheDocument();
    expect(within(inspector).queryByText(/^References/)).not.toBeInTheDocument();
    expect(within(inspector).getAllByRole("button", { name: /^Review/ }).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Ask AI about code" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Select code" }));
    expect(screen.getByRole("button", { name: "Ask selection" })).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: /Changes/ }));
    const menu = screen.getByRole("dialog", { name: "Uncommitted changes" });
    expect(within(menu).getAllByRole("button", { name: /^Review/ }).length).toBeGreaterThan(0);

    expect(recorded.urls.some((url) => url.includes("/v1/code-map/editor"))).toBe(true);
    expect(recorded.urls.filter((url) => url.includes("/v1/code-map/references"))).toEqual([]);
  });

  it("draws the module map with the import-level note and says when links are bounded", async () => {
    mockLiteApi({
      "/v1/code-map/modules": () =>
        json({ ...modules, links_truncated: true, context: liteContext, purpose_generation: { available: true, model: "m" } }),
    });
    renderLocal("/code?view=map");

    expect(await screen.findByText(importNote)).toBeInTheDocument();
    expect(screen.getByText("bounded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Describe with/ })).not.toBeInTheDocument();
  });
});
