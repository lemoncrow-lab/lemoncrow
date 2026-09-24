import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import ReviewStream from "./ReviewStream";
import type { Annotation, FileDetail, ReviewTarget } from "./types";
const pierre = vi.hoisted(() => ({
  parsePatchFiles: vi.fn((_patch: string, id: string) => {
    const name = id.startsWith("lc:") ? id.slice(3) : id;
    return [{ files: [{ id, name }] }];
  }),
}));

vi.mock("@pierre/diffs", () => ({
  parsePatchFiles: pierre.parsePatchFiles,
}));

const pierreReact = vi.hoisted(() => ({
  diffProps: new Map<string, any>(),
  fileProps: new Map<string, any>(),
}));

vi.mock("./reviewApi", () => ({
  currentReviewId: () => "review-1",
}));

vi.mock("./WebReviewPreview", () => ({
  default: (props: any) => (
    <div data-testid={`mock-web-preview:${props.documentPath}`}>
      <span>{props.preview.default_route}</span>
      <span data-testid="mock-web-service-name">{props.serviceName}</span>
      <span>{props.affectedPaths.length} files</span>
      <span data-testid="mock-web-route-count">{props.routeOptions.length} routes</span>
      {props.routeOptions[1] && (
        <button type="button" onClick={() => props.onNavigateSurface(props.routeOptions[1].surfaceKey)}>Select second web route</button>
      )}
      <button type="button" onClick={props.onOpenDedicated}>Open web surface in dedicated tab</button>
    </div>
  ),
}));

beforeEach(() => {
  window.localStorage.clear();
});

vi.mock("./MediaReviewPreview", () => ({
  default: (props: any) => (
    <div data-testid={`mock-media-preview:${props.documentPath}`}>{props.mode}</div>
  ),
}));

vi.mock("@pierre/diffs/react", () => ({
    FileDiff: (props: any) => {
      const path = props.fileDiff.name ?? String(props.fileDiff.id ?? "").replace(/^lc:/, "");
      pierreReact.diffProps.set(path, props);
      return (
        <section
          data-testid={`mock-file-diff:${path}`}
          ref={(node) => {
            if (node) {
              if (!node.querySelector("diffs-container")) {
                const host = document.createElement("diffs-container");
                const code = document.createElement("div");
                code.setAttribute("data-code", "");
                Object.defineProperty(code, "clientWidth", { configurable: true, value: 320 });
                Object.defineProperty(code, "scrollWidth", {
                  configurable: true,
                  value: path.includes("wide") ? 920 : 320,
                });
                host.appendChild(code);
                for (let line = 1; line <= 20; line += 1) {
                  const row = document.createElement("div");
                  row.setAttribute("data-line", String(line));
                  row.setAttribute("data-line-type", "addition");
                  row.setAttribute("data-testid", `scope-row:${path}:${line}`);
                  host.appendChild(row);
                }
                node.appendChild(host);
              }
              props.options?.onPostRender?.(node, null, "mount");
            }
          }}
        >
          <div data-header-content>
            {props.renderHeaderPrefix?.(props.fileDiff)}
            <span data-title>{path}</span>
            {props.renderHeaderFilenameSuffix?.(props.fileDiff)}
          </div>
          {props.renderHeaderMetadata?.(props.fileDiff)}
          {(props.lineAnnotations ?? []).map((annotation: any, index: number) => (
            <div
              key={`${annotation.side}:${annotation.lineNumber}:${index}`}
              data-testid={`marker:${path}:${annotation.side}:${annotation.lineNumber}`}
            >
              {props.renderAnnotation?.(annotation)}
            </div>
          ))}
        </section>
      );
    },
    File: (props: any) => {
      const path = props.file.name;
      pierreReact.fileProps.set(path, props);
      return (
        <section
          data-testid={`mock-file:${path}`}
          ref={(node) => {
            if (node) props.options?.onPostRender?.(node, null, "mount");
          }}
        >
          <div data-header-content>
            {props.renderHeaderPrefix?.(props.file)}
            <span data-title>{path}</span>
            {props.renderHeaderFilenameSuffix?.(props.file)}
          </div>
          {props.renderHeaderMetadata?.(props.file)}
          <pre>{props.file.contents}</pre>
        </section>
      );
    },
}));

function target(id: string, path: string, line: number): ReviewTarget {
  return {
    target_id: `target:${id}`,
    unit_key: id,
    kind: "symbol",
    path,
    label: `${path}::${id}`,
    symbol: id,
    start_line: line,
    end_line: line + 2,
    hunk_ordinals: [0],
    spans: [{ side: "new", start_line: line, end_line: line, hunk_ordinal: 0 }],
    state: "unreviewed",
    changed_since_mark: false,
    reviewed_revision_id: "",
    attention_rank: 1,
    attention_level: "high",
    reasons: ["public contract changed"],
    additions: 1,
    deletions: 1,
    fingerprint_method: "symbol_body_sha256",
    verification: { pass: 0, fail: 0, unknown: 0 },
    annotation_counts: { open: 0, orphaned: 0, addressed_needs_rereview: 0 },
  };
}

function detail(path: string): FileDetail {
  return {
    path,
    status: "modified",
    additions: 1,
    deletions: 1,
    patch: `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-old\n+new\n`,
    renderable: true,
    refusal: "",
    detail: "",
    degraded: [],
  };
}

function markdownDetail(path = "README.md"): FileDetail {
  return {
    ...detail(path),
    patch: `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1 +1 @@\n-# Old\n+# New\n`,
    preview: {
      kind: "markdown",
      old_content: "# Old\n",
      new_content: "# New\n",
    },
  };
}

function webDetail(path: string): FileDetail {
  return {
    ...detail(path),
    preview: {
      kind: "web",
      framework: "next",
      routes: ["/", "/scan"],
      default_route: "/",
    },
  };
}

/** Everything a ReviewStream needs except the two props a test is actually about. */
function streamProps(
  details: Record<string, FileDetail>,
  outstanding: [string, number][],
  activeTargetId: string,
) {
  return {
    details,
    errors: {},
    annotations: [] as Annotation[],
    diffStyle: "split" as const,
    activeTargetId,
    busy: false,
    remainingFileCount: 0,
    outstandingByPath: new Map(outstanding),
    onActiveTarget: vi.fn(),
    onDraft: vi.fn(),
    onCreate: vi.fn(),
    onSetAnnotationState: vi.fn(),
    onMark: vi.fn(),
    onComment: vi.fn(),
    onContext: vi.fn(),
    onBulkReview: vi.fn(),
    onLoadMore: vi.fn(),
  };
}

function fileAnnotation(id: string, path: string, body: string, extra: Partial<Annotation> = {}): Annotation {
  return {
    id,
    review_id: "review",
    revision_id: "revision",
    parent_id: "",
    kind: "comment",
    state: "open",
    body,
    created_by: "reviewer",
    created_by_actor: "human",
    created_at: "",
    updated_at: "",
    anchor_method: "file",
    anchor_method_label: "file",
    anchor_exact: true,
    anchor_detail: "",
    anchored: true,
    path,
    side: "new",
    start_line: 0,
    end_line: 0,
    file_level: true,
    unit_key: `file:${path}`,
    symbol: "",
    origin_symbol: "",
    ...extra,
  };
}

describe("continuous review stream", () => {
  it("keeps a synchronized horizontal scrollbar reachable for the active wide diff", async () => {
    const wide = target("wide", "wide.ts", 1);
    const common = streamProps({ "wide.ts": detail("wide.ts") }, [["wide.ts", 1]], "target:wide");
    render(<ReviewStream {...common} targets={[wide]} draft={null} />);

    const dock = await screen.findByTestId("review-horizontal-scrollbar");
    expect(dock.getAttribute("aria-label")).toBe("Horizontal scroll for wide.ts");
    const code = screen.getByTestId("mock-file-diff:wide.ts").querySelector<HTMLElement>("[data-code]");
    expect(code).toBeTruthy();

    act(() => {
      dock.scrollLeft = 240;
      dock.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    expect(code?.scrollLeft).toBe(240);

    act(() => {
      if (!code) return;
      code.scrollLeft = 125;
      code.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    await waitFor(() => expect(dock.scrollLeft).toBe(125));
  });

  it("does not add a docked scrollbar when the active diff fits horizontally", async () => {
    const only = target("fits", "fits.ts", 1);
    const common = streamProps({ "fits.ts": detail("fits.ts") }, [["fits.ts", 1]], "target:fits");
    render(<ReviewStream {...common} targets={[only]} draft={null} />);
    await waitFor(() => expect(screen.getByTestId("mock-file-diff:fits.ts")).toBeTruthy());
    expect(screen.queryByTestId("review-horizontal-scrollbar")).toBeNull();
  });

  it("stitches Markdown Preview/Compare into the same virtualized stream item", async () => {
    const only = target("readme", "README.md", 1);
    const common = streamProps({ "README.md": markdownDetail() }, [["README.md", 1]], "target:readme");
    render(<ReviewStream {...common} targets={[only]} draft={null} />);

    const stitched = screen.getByTestId("markdown-stream:README.md");
    expect(within(stitched).getByTestId("markdown-review-preview")).toBeTruthy();
    expect(within(stitched).getByRole("button", { name: "preview" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByTestId("markdown-focus-view")).toBeNull();
    expect(screen.queryByTestId("mock-file-diff:README.md")).toBeNull();
    expect(within(stitched).queryByText("Before")).toBeNull();

    await userEvent.hover(stitched);
    await userEvent.keyboard("2");
    expect(within(stitched).getByText("Before")).toBeTruthy();
    expect(within(stitched).getByText("After")).toBeTruthy();
    expect(within(stitched).getByRole("button", { name: "compare" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByTestId("mock-file-diff:README.md")).toBeNull();

    await userEvent.keyboard("3");
    expect(screen.queryByTestId("markdown-stream:README.md")).toBeNull();
    expect(screen.getByTestId("mock-file-diff:README.md")).toBeTruthy();
    expect(screen.getByTestId("mock-file-diff:README.md").querySelector('[data-file-icon="README.md"]')).toBeTruthy();
    expect(pierreReact.diffProps.get("README.md")).toBeTruthy();
    expect(screen.getByRole("button", { name: "source" }).getAttribute("aria-pressed")).toBe("true");

    await userEvent.keyboard("1");
    const previewStream = screen.getByTestId("markdown-stream:README.md");
    expect(previewStream).toBeTruthy();
    expect(screen.queryByTestId("mock-file-diff:README.md")).toBeNull();

    const themeToggle = within(previewStream).getByRole("button", { name: /Markdown preview theme: Auto/ });
    expect(within(previewStream).getByTestId("markdown-review-preview").getAttribute("data-preview-theme")).toBe("light");
    await userEvent.click(themeToggle);
    expect(within(previewStream).getByRole("button", { name: /Markdown preview theme: light/ })).toBeTruthy();
    await userEvent.click(within(previewStream).getByRole("button", { name: /Markdown preview theme: light/ }));
    expect(within(previewStream).getByTestId("markdown-review-preview").getAttribute("data-preview-theme")).toBe("dark");
  });

  it("opens files in a dedicated Review tab", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const path = "src/dedicated.py";
    const only = target("dedicated", path, 1);
    render(
      <ReviewStream
        {...streamProps({ [path]: detail(path) }, [[path, 1]], "target:dedicated")}
        targets={[only]}
        draft={null}
      />,
    );

    await userEvent.click(screen.getByText(path));
    expect(open).toHaveBeenCalledWith(
      expect.stringContaining("review-item=file"),
      "_blank",
      "noopener,noreferrer",
    );
    expect(String(open.mock.calls[0]?.[0])).toContain("review-path=src%2Fdedicated.py");

    open.mockClear();
    await userEvent.click(screen.getByRole("button", { name: `Open ${path} in dedicated tab` }));
    expect(open).toHaveBeenCalledTimes(1);
    open.mockRestore();
  });

  it("renders one shared web surface and keeps every affected file as a source diff", async () => {
    const heroPath = "src/components/Hero.tsx";
    const stylePath = "src/components/hero.css";
    const targets = [target("hero", heroPath, 1), target("style", stylePath, 1)];
    const common = streamProps(
      { [heroPath]: webDetail(heroPath), [stylePath]: webDetail(stylePath) },
      [[heroPath, 1], [stylePath, 1]],
      "target:hero",
    );
    const surfaces = [{
      id: "/",
      provider: "web",
      kind: "web.route",
      title: "/",
      locator: "/",
      runtime: "",
      affected_paths: [heroPath, stylePath],
      capabilities: ["preview", "compare", "source"],
      metadata: { framework: "next", root: "." },
    }];

    render(<ReviewStream {...common} surfaces={surfaces} targets={targets} draft={null} />);

    const surface = screen.getByTestId("web-surface:web:/");
    expect(within(surface).getByText("2 files")).toBeTruthy();
    expect(screen.getAllByTestId(/^mock-web-preview:/)).toHaveLength(1);
    expect(screen.getByTestId(`mock-file-diff:${heroPath}`)).toBeTruthy();
    expect(screen.getByTestId(`mock-file-diff:${stylePath}`)).toBeTruthy();
    expect(screen.getAllByText("surface /")).toHaveLength(2);
    expect(surface.getAttribute("data-review-surface-key")).toBe("web:/");
    expect(within(surface).queryByRole("button", { name: /source/i })).toBeNull();
    expect(screen.getAllByTestId(/^mock-web-preview:/)[0]?.textContent).toContain("2 files");
  });

  it("keeps all web routes in one surface entry and switches the rendered route in place", async () => {
    const heroPath = "src/components/Hero.tsx";
    const stylePath = "src/components/hero.css";
    const targets = [target("hero", heroPath, 1), target("style", stylePath, 1)];
    const common = streamProps(
      { [heroPath]: webDetail(heroPath), [stylePath]: webDetail(stylePath) },
      [[heroPath, 1], [stylePath, 1]],
      "target:hero",
    );
    const surfaces = [
      {
        id: "/",
        provider: "web",
        kind: "web.route",
        title: "/",
        locator: "/",
        runtime: "frontend",
        affected_paths: [heroPath, stylePath],
        capabilities: ["preview", "compare"],
        metadata: { framework: "next", root: "." },
      },
      {
        id: "/scan",
        provider: "web",
        kind: "web.route",
        title: "/scan",
        locator: "/scan",
        runtime: "frontend",
        affected_paths: [heroPath],
        capabilities: ["preview", "compare"],
        metadata: { framework: "next", root: "." },
      },
    ];

    render(<ReviewStream {...common} surfaces={surfaces} targets={targets} draft={null} />);

    expect(screen.getAllByTestId(/^mock-web-preview:/)).toHaveLength(1);
    expect(screen.getByTestId("mock-web-route-count").textContent).toBe("2 routes");
    expect(screen.getByTestId("web-surface:web:/")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Select second web route" }));
    expect(screen.getAllByTestId(/^mock-web-preview:/)).toHaveLength(1);
    expect(screen.getByTestId("web-surface:web:/scan")).toBeTruthy();
    expect(screen.getByTestId("mock-web-route-count").textContent).toBe("2 routes");
    expect(screen.getAllByTestId(/^mock-web-preview:/)[0]?.textContent).toContain("/scan");
  });

  it("defaults to the web surface that touches the highest-priority changed source", () => {
    const importantPath = "src/important.tsx";
    const laterPath = "src/later.tsx";
    const targets = [target("important", importantPath, 1), target("later", laterPath, 1)];
    const surfaces = [
      {
        id: "/",
        provider: "web",
        kind: "web.route",
        title: "/",
        locator: "/",
        runtime: "",
        affected_paths: [laterPath],
        capabilities: ["preview", "compare"],
        metadata: { framework: "next", root: ".", surface_id: "app" },
      },
      {
        id: "/important",
        provider: "web",
        kind: "web.route",
        title: "/important",
        locator: "/important",
        runtime: "",
        affected_paths: [importantPath],
        capabilities: ["preview", "compare"],
        metadata: { framework: "next", root: ".", surface_id: "app" },
      },
    ];

    render(
      <ReviewStream
        {...streamProps(
          { [importantPath]: webDetail(importantPath), [laterPath]: webDetail(laterPath) },
          [[importantPath, 1], [laterPath, 1]],
          "target:important",
        )}
        targets={targets}
        draft={null}
        surfaces={surfaces}
      />,
    );

    expect(screen.getByTestId("web-surface:web:/important")).toBeTruthy();
    expect(screen.queryByTestId("web-surface:web:/")).toBeNull();
  });

  it("opens a dedicated web workspace on the selected route while keeping all routes available", () => {
    const heroPath = "src/components/Hero.tsx";
    const stylePath = "src/components/hero.css";
    const targets = [target("hero", heroPath, 1), target("style", stylePath, 1)];
    const surfaces = [
      {
        id: "/",
        provider: "web",
        kind: "web.route",
        title: "/",
        locator: "/",
        runtime: "",
        affected_paths: [heroPath, stylePath],
        capabilities: ["preview", "compare"],
        metadata: {},
      },
      {
        id: "/scan",
        provider: "web",
        kind: "web.route",
        title: "/scan",
        locator: "/scan",
        runtime: "",
        affected_paths: [heroPath],
        capabilities: ["preview", "compare"],
        metadata: {},
      },
    ];
    render(
      <ReviewStream
        {...streamProps(
          { [heroPath]: webDetail(heroPath), [stylePath]: webDetail(stylePath) },
          [[heroPath, 1], [stylePath, 1]],
          "target:hero",
        )}
        targets={targets}
        draft={null}
        surfaces={surfaces}
        dedicatedItem={{ kind: "surface", provider: "web", id: "/scan" }}
      />,
    );

    expect(screen.getByTestId("web-surface:web:/scan")).toBeTruthy();
    expect(screen.getByTestId("mock-web-route-count").textContent).toBe("2 routes");
    expect(screen.getAllByTestId(/^mock-web-preview:/)[0]?.textContent).toContain("/scan");
    expect(screen.queryByTestId(`mock-file-diff:${heroPath}`)).toBeNull();
    expect(screen.queryByTestId(`mock-file-diff:${stylePath}`)).toBeNull();
  });

  it("renders a dedicated web surface even without source targets", () => {
    const path = "src/standalone.tsx";
    const surface = {
      id: "/standalone",
      provider: "web",
      kind: "web.route",
      title: "/standalone",
      locator: "/standalone",
      runtime: "",
      affected_paths: [path],
      capabilities: ["preview", "compare"],
      metadata: {},
    };
    render(
      <ReviewStream
        {...streamProps({ [path]: webDetail(path) }, [], "")}
        targets={[]}
        draft={null}
        surfaces={[surface]}
        dedicatedItem={{ kind: "surface", provider: "web", id: "/standalone" }}
        remainingFileCount={12}
      />,
    );

    expect(screen.getByTestId("web-surface:web:/standalone")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Load next files/ })).toBeNull();
  });

  it("renders a frozen nested web surface even when its source path is outside parent review targets", () => {
    const parentPath = "src/parent.py";
    const nestedPath = "landing/src/pages/index.astro";
    const only = target("parent", parentPath, 1);
    const surface = {
      id: "landing:/",
      provider: "web",
      kind: "web.route",
      title: "/",
      locator: "/",
      runtime: "",
      affected_paths: [nestedPath],
      capabilities: ["preview", "compare"],
      metadata: { framework: "astro", root: "landing" },
    };

    render(
      <ReviewStream
        {...streamProps({ [parentPath]: detail(parentPath) }, [[parentPath, 1]], "target:parent")}
        targets={[only]}
        draft={null}
        surfaces={[surface]}
      />,
    );

    const rendered = screen.getByTestId(`mock-web-preview:${nestedPath}`);
    expect(rendered).toBeTruthy();
    expect(within(rendered).getByText("/")).toBeTruthy();
    expect(screen.getByTestId(`mock-file-diff:${parentPath}`)).toBeTruthy();
  });

  it("renders one API surface before its source and runs Preview/Compare explicitly", async () => {
    const path = "bruno/Hello.bru";
    const only = target("hello", path, 1);
    const common = streamProps({ [path]: detail(path) }, [[path, 1]], "target:hello");
    const surface = {
      id: path,
      provider: "bruno",
      kind: "api.request",
      title: "Hello reviewer",
      locator: "GET {{baseUrl}}/api/hello?name=Ada",
      runtime: "demo-api",
      affected_paths: [path],
      capabilities: ["source", "execute", "results", "compare"],
      metadata: { method: "GET", url: "{{baseUrl}}/api/hello?name=Ada" },
    };
    const onRunSurface = vi.fn();
    const open = vi.spyOn(window, "open").mockImplementation(() => null);

    render(
      <ReviewStream
        {...common}
        targets={[only]}
        draft={null}
        surfaces={[surface]}
        onRunSurface={onRunSurface}
      />,
    );

    const apiSurface = screen.getByTestId(`api-surface:bruno:${path}`);
    expect(apiSurface).toBeTruthy();
    expect(screen.getAllByTestId(`api-surface:bruno:${path}`)).toHaveLength(1);
    expect(screen.getByTestId(`mock-file-diff:${path}`)).toBeTruthy();
    expect(within(apiSurface).getByText("GET {{baseUrl}}/api/hello?name=Ada")).toBeTruthy();
    await userEvent.click(within(apiSurface).getByRole("button", { name: "Open API surface Hello reviewer in dedicated tab" }));
    expect(open).toHaveBeenCalledWith(expect.stringContaining("review-item=surface"), "_blank", "noopener,noreferrer");
    expect(String(open.mock.calls[0]?.[0])).toContain("review-provider=bruno");

    await userEvent.click(within(apiSurface).getByRole("button", { name: "Run request" }));
    expect(onRunSurface).toHaveBeenCalledWith(surface, false);

    await userEvent.hover(apiSurface);
    await userEvent.keyboard("2");
    expect(within(apiSurface).getAllByText("Not run yet")).toHaveLength(2);
    expect(within(apiSurface).getByRole("button", { name: "compare" }).getAttribute("aria-pressed")).toBe("true");
    await userEvent.keyboard("1");
    expect(within(apiSurface).getAllByText("Not run yet")).toHaveLength(1);
    expect(within(apiSurface).getByRole("button", { name: "preview" }).getAttribute("aria-pressed")).toBe("true");

    await userEvent.click(within(apiSurface).getByRole("button", { name: "compare" }));
    expect(within(apiSurface).getAllByText("Not run yet")).toHaveLength(2);
    await userEvent.click(within(apiSurface).getByRole("button", { name: "Run compare" }));
    expect(onRunSurface).toHaveBeenCalledWith(surface, true);
    expect(screen.getByTestId(`mock-file-diff:${path}`)).toBeTruthy();
    open.mockRestore();
  });

  it("keeps API endpoints in one searchable surface and preserves the filter between selections", async () => {
    const helloPath = "bruno/Hello.bru";
    const healthPath = "bruno/Health.bru";
    const targets = [target("hello", helloPath, 1), target("health", healthPath, 1)];
    const common = streamProps(
      { [helloPath]: detail(helloPath), [healthPath]: detail(healthPath) },
      [[helloPath, 1], [healthPath, 1]],
      "target:hello",
    );
    const hello = {
      id: helloPath,
      provider: "bruno",
      kind: "api.request",
      title: "Hello reviewer",
      locator: "GET {{baseUrl}}/api/hello",
      runtime: "demo-api",
      affected_paths: [helloPath],
      capabilities: ["source", "execute", "results", "compare"],
      metadata: { method: "GET", url: "{{baseUrl}}/api/hello" },
    };
    const health = {
      id: healthPath,
      provider: "bruno",
      kind: "api.request",
      title: "Health check",
      locator: "POST {{baseUrl}}/api/health",
      runtime: "demo-api",
      affected_paths: [healthPath],
      capabilities: ["source", "execute", "results", "compare"],
      metadata: { method: "POST", url: "{{baseUrl}}/api/health" },
    };
    const onRunSurface = vi.fn();

    render(
      <ReviewStream
        {...common}
        targets={targets}
        draft={null}
        surfaces={[hello, health]}
        onRunSurface={onRunSurface}
      />,
    );

    expect(screen.getAllByTestId(/^api-surface:/)).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "API endpoint" }));
    const search = screen.getByRole("searchbox", { name: "API endpoint search" });
    await userEvent.type(search, "health");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByRole("option").textContent).toContain("POST {{baseUrl}}/api/health");
    await userEvent.click(screen.getByRole("option"));

    const selected = screen.getByTestId(`api-surface:bruno:${healthPath}`);
    expect(screen.getAllByTestId(/^api-surface:/)).toHaveLength(1);
    expect(within(selected).getByRole("button", { name: "API endpoint" }).textContent).toContain("POST {{baseUrl}}/api/health");
    await userEvent.click(within(selected).getByRole("button", { name: "Run request" }));
    expect(onRunSurface).toHaveBeenCalledWith(health, false);

    await userEvent.click(screen.getByRole("button", { name: "API endpoint" }));
    expect((screen.getByRole("searchbox", { name: "API endpoint search" }) as HTMLInputElement).value).toBe("health");
    expect(screen.getAllByRole("option")).toHaveLength(1);
  });

  it("keeps product surfaces above source files and names their configured services", async () => {
    const sourcePath = "src/core.py";
    const webPath = "frontend/src/app/page.tsx";
    const apiPath = "backend/bruno/Health.bru";
    const targets = [
      target("source", sourcePath, 1),
      target("web", webPath, 1),
      target("api", apiPath, 1),
    ];
    const web = {
      id: "frontend:/",
      provider: "web",
      kind: "web.route",
      title: "/",
      locator: "/",
      runtime: "",
      affected_paths: [webPath],
      capabilities: ["preview", "compare", "source"],
      metadata: { framework: "next", root: "frontend", surface_id: "app" },
    };
    const api = {
      id: apiPath,
      provider: "bruno",
      kind: "api.request",
      title: "Health",
      locator: "GET {{baseUrl}}/health",
      runtime: "api-runtime",
      affected_paths: [apiPath],
      capabilities: ["source", "execute", "results", "compare"],
      metadata: { method: "GET", url: "{{baseUrl}}/health", surface_id: "api" },
    };
    const service = {
      id: "app-stack:api",
      provider: "service",
      kind: "service",
      title: "api",
      locator: "api",
      runtime: "app-stack",
      affected_paths: [sourcePath, apiPath],
      capabilities: ["source", "execute", "results", "compare"],
      metadata: { surface_id: "app-stack", service: "api", runner: "docker-compose" },
    };
    const frontendService = {
      ...service,
      id: "app-stack:frontend",
      title: "frontend",
      locator: "frontend",
      affected_paths: [webPath],
      metadata: { ...service.metadata, service: "frontend" },
    };
    const onRunSurface = vi.fn();

    render(
      <ReviewStream
        {...streamProps(
          { [sourcePath]: detail(sourcePath), [webPath]: webDetail(webPath), [apiPath]: detail(apiPath) },
          [[sourcePath, 1], [webPath, 1], [apiPath, 1]],
          "target:source",
        )}
        targets={targets}
        draft={null}
        surfaces={[web, api, service, frontendService]}
        onRunSurface={onRunSurface}
      />,
    );

    const itemOrder = [...screen.getByTestId("review-stream").querySelectorAll<HTMLElement>("[data-review-stream-item]")]
      .map((node) => node.dataset.reviewStreamItem);
    expect(itemOrder.slice(0, 4)).toEqual([
      "surface-group:web",
      "surface-group:api",
      "surface:service:app-stack:api",
      "surface:service:app-stack:frontend",
    ]);
    expect(screen.getByTestId("mock-web-service-name").textContent).toBe("app");

    const apiSurface = screen.getByTestId(`api-surface:bruno:${apiPath}`);
    expect(within(apiSurface).getByRole("button", { name: "Open API surface Health in dedicated tab" }).textContent).toContain("API · api");
    const runtimeSurface = screen.getByTestId("service-surface:service:app-stack:api");
    const frontendRuntimeSurface = screen.getByTestId("service-surface:service:app-stack:frontend");
    expect(within(runtimeSurface).getAllByText("api").length).toBeGreaterThan(0);
    expect(within(frontendRuntimeSurface).getAllByText("frontend").length).toBeGreaterThan(0);
    expect(within(runtimeSurface).getByRole("button", { name: "Open runtime surface api in dedicated tab" }).textContent).toContain("Docker · app-stack");
    expect(runtimeSurface.getAttribute("data-review-surface-key")).toBe("service:app-stack:api");
    expect(frontendRuntimeSurface.getAttribute("data-review-surface-key")).toBe("service:app-stack:frontend");
    expect(screen.queryByRole("button", { name: "Runtime service" })).toBeNull();
    await userEvent.click(within(runtimeSurface).getByRole("button", { name: "Run check" }));
    expect(onRunSurface).toHaveBeenCalledWith(service, false);
    await userEvent.click(within(frontendRuntimeSurface).getByRole("button", { name: "Run check" }));
    expect(onRunSurface).toHaveBeenCalledWith(frontendService, false);
    expect(onRunSurface).toHaveBeenCalledWith(service, false);

    expect(screen.getByTestId(`mock-file-diff:${sourcePath}`)).toBeTruthy();
    expect(screen.getByTestId(`mock-file-diff:${webPath}`)).toBeTruthy();
    expect(screen.getByTestId(`mock-file-diff:${apiPath}`)).toBeTruthy();
  });

  it("opens a source-anchored comment draft from the stitched rendered Markdown document", async () => {
    const only = target("readme", "README.md", 1);
    const onDraft = vi.fn();
    const common = {
      ...streamProps({ "README.md": markdownDetail() }, [["README.md", 1]], "target:readme"),
      onDraft,
    };
    render(<ReviewStream {...common} targets={[only]} draft={null} />);

    const stitched = screen.getByTestId("markdown-stream:README.md");
    const after = within(stitched).getByTestId("markdown-additions");
    await userEvent.click(within(after).getByRole("heading", { name: "New" }));
    expect(onDraft).toHaveBeenCalledWith({
      path: "README.md",
      startLine: 1,
      endLine: 1,
      side: "additions",
      targetUnitKey: "readme",
    });
  });

  it("opens one comment draft for the full selected line range", () => {
    const only = target("range", "src/range.py", 3);
    const onDraft = vi.fn();
    const onActiveTarget = vi.fn();
    render(
      <ReviewStream
        {...streamProps({ "src/range.py": detail("src/range.py") }, [["src/range.py", 1]], only.target_id)}
        targets={[only]}
        draft={null}
        onDraft={onDraft}
        onActiveTarget={onActiveTarget}
      />,
    );

    const options = pierreReact.diffProps.get("src/range.py")?.options;
    const range = { start: 3, end: 6, side: "additions", endSide: "additions" };
    act(() => options.onLineSelectionChange(range));
    expect(onDraft).not.toHaveBeenCalled();
    expect(pierreReact.diffProps.get("src/range.py")?.selectedLines).toEqual(range);
    act(() => options.onLineSelected(range));
    expect(onActiveTarget).toHaveBeenLastCalledWith(only.target_id);
    expect(onDraft).toHaveBeenLastCalledWith({
      path: "src/range.py",
      startLine: 3,
      endLine: 6,
      side: "additions",
      targetUnitKey: "range",
    });
  });

  it("renders several file diffs in one CodeView with target boundaries", async () => {
    const targets = [target("a_one", "src/a.py", 1), target("a_two", "src/a.py", 2), target("b_one", "src/b.py", 1)];
    const onMark = vi.fn();
    const onBulkReview = vi.fn();
    render(
      <ReviewStream
        targets={targets}
        details={{ "src/a.py": detail("src/a.py"), "src/b.py": detail("src/b.py") }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="split"
        activeTargetId="target:a_one"
        busy={false}
        remainingFileCount={0}
        outstandingByPath={new Map([["src/a.py", 2], ["src/b.py", 1]])}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={onMark}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={onBulkReview}
        onLoadMore={vi.fn()}
      />,
    );

    expect(screen.getByTestId("code-item:src/a.py")).toBeTruthy();
    expect(screen.getByTestId("code-item:src/b.py")).toBeTruthy();
    expect(screen.getByText("a_one")).toBeTruthy();
    expect(screen.getByText("a_two")).toBeTruthy();
    expect(screen.getByText("b_one")).toBeTruthy();
    // Nothing is filtered here, so neither header owes the reader a second
    // number: what the stream shows is everything the file holds.
    expect(screen.getByText("2 left")).toBeTruthy();
    expect(screen.getByText("1 left")).toBeTruthy();

    const fileHeader = screen.getByTestId("mock-file-diff:src/a.py").querySelector<HTMLElement>("[data-header-content]");
    const fileTitle = screen.getByTestId("mock-file-diff:src/a.py").querySelector<HTMLElement>("[data-title]");
    expect(fileHeader?.style.cursor).toBe("pointer");
    expect(fileTitle?.style.textDecorationLine).toBe("");
    expect(fileTitle?.style.textUnderlineOffset).toBe("2px");
    await userEvent.hover(fileHeader!);
    expect(fileTitle?.style.textDecorationLine).toBe("underline");
    await userEvent.unhover(fileHeader!);
    expect(fileTitle?.style.textDecorationLine).toBe("");

    await userEvent.click(screen.getByRole("button", { name: "Mark a_one reviewed" }));
    expect(onMark).toHaveBeenCalledWith(targets[0], "reviewed", true);

    const fileActions = screen.getByLabelText("File actions for src/a.py");
    expect(fileActions.closest("details")?.hasAttribute("open")).toBe(false);
    await userEvent.click(fileActions);
    expect(fileActions.closest("details")?.hasAttribute("open")).toBe(true);
    const bulk = screen.getByRole("menuitem", { name: "Mark safe items reviewed in src/a.py" });
    expect(bulk.getAttribute("title")).toContain("Anything needing individual attention is skipped");
    expect(within(bulk).getByText("Mark safe items reviewed")).toBeTruthy();
    await userEvent.click(bulk);
    expect(onBulkReview).toHaveBeenCalledWith("src/a.py");
  });

  it("renders the target header before its diff scope and closes a subtle outer box", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const element = this;
      if (element.dataset.testid === "code-item:src/scope.py") {
        return { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400, toJSON() {} };
      }
      if (element.dataset.reviewTargetId === "target:scope") {
        const expanded = element.style.width === "330px";
        return expanded
          ? { x: 400, y: 40, left: 400, top: 40, right: 800, bottom: 72, width: 400, height: 32, toJSON() {} }
          : { x: 470, y: 40, left: 470, top: 40, right: 770, bottom: 72, width: 300, height: 32, toJSON() {} };
      }
      if (element.dataset.reviewTargetEnd === "target:scope") {
        return { x: 0, y: 160, left: 0, top: 160, right: 0, bottom: 160, width: 0, height: 0, toJSON() {} };
      }
      return { x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() {} };
    });
    const scoped = {
      ...target("scope", "src/scope.py", 3),
      start_line: 3,
      end_line: 5,
      additions: 1,
      deletions: 0,
      // Only one changed line belongs to the raw diff span, but the ReviewTarget
      // is the whole definition. The scope box must extend through line 5.
      spans: [{ side: "new" as const, start_line: 3, end_line: 3, hunk_ordinal: 0 }],
    };
    const common = streamProps({ "src/scope.py": detail("src/scope.py") }, [["src/scope.py", 1]], scoped.target_id);

    render(<ReviewStream {...common} targets={[scoped]} draft={null} />);

    expect(screen.getByText("· covers lines 3–5")).toBeTruthy();
    const props = pierreReact.diffProps.get("src/scope.py");
    expect(props.lineAnnotations).toEqual(expect.arrayContaining([
      expect.objectContaining({ side: "additions", lineNumber: 2 }),
    ]));

    const header = screen.getByRole("group", { name: "scope review target" }) as HTMLElement;
    expect(header.className).toContain("rounded-t-md");
    expect(header.className).toContain("rounded-b-none");
    expect(header.style.left).toBe("0px");
    expect(header.style.width).toBe("330px");

    const wrapper = screen.getByTestId("code-item:src/scope.py");
    const overlay = wrapper.querySelector<HTMLElement>('[data-lc-review-scope-overlay="target:scope"]');
    expect(overlay).toBeTruthy();
    expect(overlay?.style.borderLeft).toContain("1px solid");
    expect(overlay?.style.borderRight).toContain("1px solid");
    expect(overlay?.style.borderBottom).toContain("1px solid");
    expect(overlay?.style.left).toBe("470px");
    expect(overlay?.style.width).toBe("330px");
    expect(overlay?.style.top).toBe("72px");
    expect(overlay?.style.height).toBe("63px");
    rect.mockRestore();
  });

  it("keeps carved-up parent symbols header-only so they cannot overlap child targets", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const element = this;
      if (element.dataset.testid === "code-item:src/parent.ts") {
        return { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400, toJSON() {} };
      }
      if (element.dataset.reviewTargetId === "target:parent") {
        const expanded = element.style.width === "730px";
        return expanded
          ? { x: 70, y: 40, left: 70, top: 40, right: 800, bottom: 72, width: 730, height: 32, toJSON() {} }
          : { x: 70, y: 40, left: 70, top: 40, right: 770, bottom: 72, width: 700, height: 32, toJSON() {} };
      }
      return { x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() {} };
    });
    const parent = {
      ...target("parent", "src/parent.ts", 1),
      target_id: "target:parent",
      kind: "symbol" as const,
      symbol: "parent",
      additions: 8,
      deletions: 0,
      start_line: 1,
      end_line: 20,
      spans: [{ side: "new" as const, start_line: 1, end_line: 20, hunk_ordinal: 0 }],
    };
    const child = {
      ...target("child", "src/parent.ts", 5),
      target_id: "target:child",
      kind: "symbol" as const,
      symbol: "parent.child",
      additions: 6,
      deletions: 0,
      start_line: 5,
      end_line: 10,
      spans: [{ side: "new" as const, start_line: 5, end_line: 10, hunk_ordinal: 0 }],
    };
    const common = streamProps({ "src/parent.ts": detail("src/parent.ts") }, [["src/parent.ts", 1]], parent.target_id);

    render(<ReviewStream {...common} diffStyle="unified" targets={[parent, child]} draft={null} />);

    const header = screen.getByRole("group", { name: "parent review target" });
    expect(header.className).toContain("rounded-md");
    expect(header.className).not.toContain("rounded-b-none");
    const wrapper = screen.getByTestId("code-item:src/parent.ts");
    expect(wrapper.querySelectorAll('[data-lc-review-scope-overlay="target:parent"]')).toHaveLength(0);
    rect.mockRestore();
  });

  it("keeps multi-region fallback targets header-only so they cannot enclose semantic targets", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const element = this;
      if (element.dataset.testid === "code-item:src/disjoint.ts") {
        return { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400, toJSON() {} };
      }
      if (element.dataset.reviewTargetId === "target:disjoint") {
        const expanded = element.style.width === "730px";
        return expanded
          ? { x: 70, y: 40, left: 70, top: 40, right: 800, bottom: 72, width: 730, height: 32, toJSON() {} }
          : { x: 70, y: 40, left: 70, top: 40, right: 770, bottom: 72, width: 700, height: 32, toJSON() {} };
      }
      return { x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() {} };
    });
    const disjoint = {
      ...target("disjoint", "src/disjoint.ts", 1),
      target_id: "target:disjoint",
      kind: "hunk" as const,
      symbol: "",
      additions: 4,
      deletions: 0,
      start_line: 1,
      end_line: 11,
      spans: [
        { side: "new" as const, start_line: 1, end_line: 2, hunk_ordinal: 0 },
        { side: "new" as const, start_line: 10, end_line: 11, hunk_ordinal: 0 },
      ],
    };
    const common = streamProps({ "src/disjoint.ts": detail("src/disjoint.ts") }, [["src/disjoint.ts", 1]], disjoint.target_id);

    render(<ReviewStream {...common} diffStyle="unified" targets={[disjoint]} draft={null} />);

    const header = screen.getByRole("group", { name: /Uncovered changes · 2 regions review target/i });
    expect(header.className).toContain("rounded-md");
    expect(header.className).not.toContain("rounded-b-none");

    const wrapper = screen.getByTestId("code-item:src/disjoint.ts");
    expect(wrapper.querySelectorAll('[data-lc-review-scope-overlay="target:disjoint"]')).toHaveLength(0);
    rect.mockRestore();
  });

  it("forces a completely added file to one full-width surface even when global mode is split", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const element = this;
      if (element.dataset.testid === "code-item:src/new.ts") {
        return { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400, toJSON() {} };
      }
      if (element.dataset.reviewTargetId === "target:new") {
        const expanded = element.style.width === "800px";
        return expanded
          ? { x: 0, y: 40, left: 0, top: 40, right: 800, bottom: 72, width: 800, height: 32, toJSON() {} }
          : { x: 70, y: 40, left: 70, top: 40, right: 770, bottom: 72, width: 700, height: 32, toJSON() {} };
      }
      if (element.dataset.reviewTargetEnd === "target:new") {
        return { x: 0, y: 160, left: 0, top: 160, right: 0, bottom: 160, width: 0, height: 0, toJSON() {} };
      }
      return { x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() {} };
    });
    const added = {
      ...target("new", "src/new.ts", 1),
      target_id: "target:new",
      additions: 5,
      deletions: 0,
      start_line: 1,
      end_line: 5,
      spans: [{ side: "new" as const, start_line: 1, end_line: 5, hunk_ordinal: 0 }],
    };
    const addedDetail = {
      ...detail("src/new.ts"),
      status: "added",
      additions: 5,
      deletions: 0,
    };
    const common = streamProps({ "src/new.ts": addedDetail }, [["src/new.ts", 1]], added.target_id);

    render(<ReviewStream {...common} diffStyle="split" targets={[added]} draft={null} />);

    const props = pierreReact.diffProps.get("src/new.ts");
    expect(props.options.diffStyle).toBe("unified");

    const header = screen.getByRole("group", { name: "new review target" }) as HTMLElement;
    expect(header.style.left).toBe("-70px");
    expect(header.style.width).toBe("800px");

    const wrapper = screen.getByTestId("code-item:src/new.ts");
    const overlay = wrapper.querySelector<HTMLElement>('[data-lc-review-scope-overlay="target:new"]');
    expect(overlay).toBeTruthy();
    expect(overlay?.style.left).toBe("0px");
    expect(overlay?.style.width).toBe("800px");
    expect(overlay?.style.borderLeft).toContain("1px solid");
    expect(overlay?.style.borderRight).toContain("1px solid");
    rect.mockRestore();
  });

  it("expands a unified scope across the entire diff surface including its gutter", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const element = this;
      if (element.dataset.testid === "code-item:src/unified.ts") {
        return { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400, toJSON() {} };
      }
      if (element.dataset.reviewTargetId === "target:unified") {
        const expanded = element.style.width === "800px";
        return expanded
          ? { x: 0, y: 40, left: 0, top: 40, right: 800, bottom: 72, width: 800, height: 32, toJSON() {} }
          : { x: 70, y: 40, left: 70, top: 40, right: 770, bottom: 72, width: 700, height: 32, toJSON() {} };
      }
      if (element.dataset.reviewTargetEnd === "target:unified") {
        return { x: 0, y: 160, left: 0, top: 160, right: 0, bottom: 160, width: 0, height: 0, toJSON() {} };
      }
      return { x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() {} };
    });
    const unified = {
      ...target("unified", "src/unified.ts", 3),
      target_id: "target:unified",
      additions: 3,
      deletions: 0,
      start_line: 3,
      end_line: 5,
      spans: [{ side: "new" as const, start_line: 3, end_line: 5, hunk_ordinal: 0 }],
    };
    const common = streamProps({ "src/unified.ts": detail("src/unified.ts") }, [["src/unified.ts", 1]], unified.target_id);

    render(<ReviewStream {...common} diffStyle="unified" targets={[unified]} draft={null} />);

    const header = screen.getByRole("group", { name: "unified review target" }) as HTMLElement;
    expect(header.style.left).toBe("-70px");
    expect(header.style.width).toBe("800px");

    const wrapper = screen.getByTestId("code-item:src/unified.ts");
    const overlay = wrapper.querySelector<HTMLElement>('[data-lc-review-scope-overlay="target:unified"]');
    expect(overlay).toBeTruthy();
    expect(overlay?.style.left).toBe("0px");
    expect(overlay?.style.width).toBe("800px");
    expect(overlay?.style.top).toBe("72px");
    expect(overlay?.style.height).toBe("63px");
    expect(overlay?.style.borderLeft).toContain("1px solid");
    expect(overlay?.style.borderRight).toContain("1px solid");
    rect.mockRestore();
  });

  it("keeps a modified header inside its split column while outlining both sides", () => {
    const rect = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const element = this;
      if (element.dataset.testid === "code-item:src/modified.ts") {
        return { x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 400, width: 800, height: 400, toJSON() {} };
      }
      if (element.dataset.reviewTargetId === "target:modified") {
        const expanded = element.style.width === "800px";
        return expanded
          ? { x: 0, y: 40, left: 0, top: 40, right: 800, bottom: 72, width: 800, height: 32, toJSON() {} }
          : { x: 400, y: 40, left: 400, top: 40, right: 800, bottom: 72, width: 400, height: 32, toJSON() {} };
      }
      if (element.dataset.reviewTargetEnd === "target:modified") {
        const top = element.dataset.reviewTargetRegionSide === "old" ? 140 : 180;
        return { x: 0, y: top, left: 0, top, right: 0, bottom: top, width: 0, height: 0, toJSON() {} };
      }
      return { x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() {} };
    });
    const modified = {
      ...target("modified", "src/modified.ts", 3),
      target_id: "target:modified",
      start_line: 3,
      end_line: 5,
      additions: 3,
      deletions: 2,
      spans: [
        { side: "old" as const, start_line: 3, end_line: 4, hunk_ordinal: 0 },
        { side: "new" as const, start_line: 3, end_line: 5, hunk_ordinal: 0 },
      ],
    };
    const common = streamProps({ "src/modified.ts": detail("src/modified.ts") }, [["src/modified.ts", 1]], modified.target_id);

    render(<ReviewStream {...common} targets={[modified]} draft={null} />);

    expect(screen.getByText("· modified · old 3–4 · new 3–5")).toBeTruthy();
    const header = screen.getByRole("group", { name: "modified review target" }) as HTMLElement;
    expect(header.style.left).toBe("0px");
    expect(header.style.width).toBe("400px");

    const wrapper = screen.getByTestId("code-item:src/modified.ts");
    const oldOverlay = wrapper.querySelector<HTMLElement>(
      '[data-lc-review-scope-overlay="target:modified"][data-lc-review-scope-overlay-side="old"]',
    );
    const newOverlay = wrapper.querySelector<HTMLElement>(
      '[data-lc-review-scope-overlay="target:modified"][data-lc-review-scope-overlay-side="new"]',
    );
    expect(oldOverlay).toBeTruthy();
    expect(newOverlay).toBeTruthy();

    expect(oldOverlay?.style.left).toBe("0px");
    expect(oldOverlay?.style.width).toBe("400px");
    expect(oldOverlay?.style.top).toBe("72px");
    expect(oldOverlay?.style.height).toBe("42px");
    expect(oldOverlay?.style.borderLeft).toContain("1px solid");
    expect(oldOverlay?.style.borderRight).toContain("0px");
    expect(oldOverlay?.style.borderBottom).toContain("1px solid");

    expect(newOverlay?.style.left).toBe("400px");
    expect(newOverlay?.style.width).toBe("400px");
    expect(newOverlay?.style.top).toBe("72px");
    expect(newOverlay?.style.height).toBe("63px");
    expect(newOverlay?.style.borderLeft).toContain("0px");
    expect(newOverlay?.style.borderRight).toContain("1px solid");
    expect(newOverlay?.style.borderBottom).toContain("1px solid");

    rect.mockRestore();
  });

  it("applies the selected syntax theme independently from rendered previews", () => {
    const path = "src/theme.py";
    const only = target("theme", path, 1);
    const common = streamProps({ [path]: detail(path) }, [[path, 1]], "target:theme");
    const { rerender } = render(
      <ReviewStream
        {...common}
        targets={[only]}
        draft={null}
        codeTheme="github-dark"
        chromeTheme="dark"
      />,
    );

    expect(pierreReact.diffProps.get(path).options).toMatchObject({
      theme: "github-dark-default",
      themeType: "dark",
    });

    rerender(
      <ReviewStream
        {...common}
        targets={[only]}
        draft={null}
        codeTheme="lemoncrow"
        chromeTheme="light"
      />,
    );
    expect(pierreReact.diffProps.get(path).options).toMatchObject({
      theme: "pierre-light",
      themeType: "light",
    });
  });

  it("keeps parsed diff identity stable across judgment clicks and versions real item payload changes", async () => {
    pierre.parsePatchFiles.mockClear();
    const first = target("stable", "src/stable.py", 10);
    const common = {
      details: { "src/stable.py": detail("src/stable.py") },
      errors: {},
      annotations: [],
      diffStyle: "split" as const,
      activeTargetId: "target:stable",
      busy: false,
      remainingFileCount: 0,
      outstandingByPath: new Map([["src/stable.py", 1]]),
      onActiveTarget: vi.fn(),
      onDraft: vi.fn(),
      onCreate: vi.fn(),
      onSetAnnotationState: vi.fn(),
      onMark: vi.fn(),
      onComment: vi.fn(),
      onContext: vi.fn(),
      onBulkReview: vi.fn(),
      onLoadMore: vi.fn(),
    };
    const draft = {
      path: "src/stable.py",
      startLine: 1,
      endLine: 1,
      side: "additions" as const,
      targetUnitKey: first.unit_key,
    };
    const { rerender } = render(<ReviewStream {...common} targets={[first]} draft={draft} />);
    expect(screen.getByTestId("mock-file-diff:src/stable.py")).toBeTruthy();
    const initial = pierreReact.diffProps.get("src/stable.py");
    expect(initial).toBeTruthy();
    expect(initial.lineAnnotations[0]).toMatchObject({ side: "additions", lineNumber: 1 });
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("marker:src/stable.py:additions:1")).toBeTruthy();

    rerender(<ReviewStream {...common} targets={[{ ...first, state: "reviewed" }]} draft={draft} />);
    const afterJudgment = pierreReact.diffProps.get("src/stable.py");
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
    expect(afterJudgment.fileDiff).toBe(initial.fileDiff);
    expect(screen.getByTestId("marker:src/stable.py:additions:1")).toBeTruthy();

    rerender(
      <ReviewStream
        {...common}
        targets={[{ ...first, state: "reviewed" }]}
        draft={{ ...draft, startLine: 2, endLine: 2 }}
      />,
    );
    const afterMarkerMove = pierreReact.diffProps.get("src/stable.py");
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
    expect(afterMarkerMove.fileDiff).toBe(initial.fileDiff);
    expect(screen.queryByTestId("marker:src/stable.py:additions:1")).toBeNull();
    expect(screen.getByTestId("marker:src/stable.py:additions:2")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Collapse src/stable.py" }));
    expect(screen.getByTestId("code-item:src/stable.py").dataset.collapsed).toBe("true");
    expect(screen.queryByTestId("mock-file-diff:src/stable.py")).toBeNull();
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Expand src/stable.py" }));
    const afterExpand = pierreReact.diffProps.get("src/stable.py");
    expect(screen.getByTestId("code-item:src/stable.py").dataset.collapsed).toBe("false");
    expect(afterExpand.fileDiff).toBe(initial.fileDiff);
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
  });

  it("hands a moved marker to the viewer even when the signature keeps its length", () => {
    const only = target("stable", "src/stable.py", 10);
    const common = streamProps({ "src/stable.py": detail("src/stable.py") }, [["src/stable.py", 1]], "target:stable");
    const draft = {
      path: "src/stable.py",
      startLine: 1,
      endLine: 1,
      side: "additions" as const,
      targetUnitKey: only.unit_key,
    };
    const { rerender } = render(<ReviewStream {...common} targets={[only]} draft={draft} />);
    expect(screen.getByTestId("marker:src/stable.py:additions:1")).toBeTruthy();

    // `additions:1-1` and `additions:2-2` are the same number of characters, so
    // a version derived from the signature's *length* cannot tell the two marker
    // states apart and the viewer keeps the record it already has — the composer
    // stays stranded on line 1 while the reader is looking at line 2.
    rerender(<ReviewStream {...common} targets={[only]} draft={{ ...draft, startLine: 2, endLine: 2 }} />);
    expect(screen.queryByTestId("marker:src/stable.py:additions:1")).toBeNull();
    expect(screen.getByTestId("marker:src/stable.py:additions:2")).toBeTruthy();
  });

  it("collapses and expands a file for real", async () => {
    pierre.parsePatchFiles.mockClear();
    const only = target("stable", "src/stable.py", 10);
    const common = streamProps({ "src/stable.py": detail("src/stable.py") }, [["src/stable.py", 1]], "target:stable");
    render(<ReviewStream {...common} targets={[only]} draft={null} />);
    const section = () => screen.getByTestId("code-item:src/stable.py");

    expect(section().dataset.collapsed).toBe("false");
    expect(screen.getByTestId("mock-file-diff:src/stable.py")).toBeTruthy();
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Collapse src/stable.py" }));
    expect(section().dataset.collapsed).toBe("true");
    expect(screen.queryByTestId("mock-file-diff:src/stable.py")).toBeNull();
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Expand src/stable.py" }));
    expect(section().dataset.collapsed).toBe("false");
    expect(screen.getByTestId("mock-file-diff:src/stable.py")).toBeTruthy();
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(1);
  });

  it("drops stale per-file render state when search removes and later re-adds a file", () => {
    pierre.parsePatchFiles.mockClear();
    const stable = target("stable", "src/stable.py", 10);
    const other = target("other", "src/other.py", 4);
    const common = streamProps(
      { "src/stable.py": detail("src/stable.py"), "src/other.py": detail("src/other.py") },
      [["src/stable.py", 1], ["src/other.py", 1]],
      "target:other",
    );
    const draft = { path: "src/stable.py", startLine: 1, endLine: 1, side: "additions" as const };

    const { rerender } = render(<ReviewStream {...common} targets={[stable, other]} draft={draft} />);
    expect(screen.getByTestId("marker:src/stable.py:additions:1")).toBeTruthy();
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(2);

    rerender(<ReviewStream {...common} targets={[other]} draft={null} />);
    expect(screen.queryByTestId("code-item:src/stable.py")).toBeNull();
    expect(screen.getByTestId("code-item:src/other.py")).toBeTruthy();
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(2);

    rerender(<ReviewStream {...common} targets={[stable, other]} draft={{ ...draft, startLine: 5, endLine: 5 }} />);
    expect(pierre.parsePatchFiles).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("marker:src/stable.py:additions:5")).toBeTruthy();
    expect(screen.queryByTestId("marker:src/stable.py:additions:1")).toBeNull();
    expect(screen.getByTestId("code-item:src/stable.py")).toBeTruthy();
  });

  it("leaves LemonCrow's attention note out of the diff's comment column", () => {
    const only = target("a_one", "src/a.py", 1);
    const common = streamProps({ "src/a.py": detail("src/a.py") }, [["src/a.py", 1]], "target:a_one");
    // Exactly what the server writes for an attention note: preparation.py's
    // project_lemoncrow_annotations passes created_by="lemoncrow" and
    // created_by_actor="unknown", never a human actor.
    const note = fileAnnotation("note", "src/a.py", "• public contract changed", {
      source: "lemoncrow",
      created_by: "lemoncrow",
      created_by_actor: "unknown",
      title: "Why this file deserves attention",
    });
    const human = fileAnnotation("human", "src/a.py", "Please rename this module", { source: "human" });
    render(<ReviewStream {...common} targets={[only]} draft={null} annotations={[note, human]} />);

    expect(screen.getByText("Please rename this module")).toBeTruthy();
    expect(screen.queryByText("• public contract changed")).toBeNull();
  });

  it("refuses to pass a search-narrowed count off as the file's own", () => {
    const only = target("a_one", "src/a.py", 1);
    // What the reader's search left in the stream is one of the seven targets
    // src/a.py still owes a judgment. `outstandingByPath` is the caller's
    // whole-review tally and is not narrowed by the search, so the header can
    // prove the stream is showing a slice without being told a second time.
    const common = streamProps({ "src/a.py": detail("src/a.py") }, [["src/a.py", 7]], "target:a_one");
    render(<ReviewStream {...common} targets={[only]} draft={null} />);
    expect(screen.queryByText("0/1 targets")).toBeNull();
    expect(screen.getByText("1 shown · 7 left in file")).toBeTruthy();
  });

  it("keeps Pierre FileDiff options stable when only the active target changes", () => {
    const targets = [target("a", "src/a.py", 1), target("b", "src/a.py", 20)];
    const common = {
      targets,
      details: { "src/a.py": detail("src/a.py") },
      errors: {},
      annotations: [],
      draft: null,
      diffStyle: "split" as const,
      busy: false,
      remainingFileCount: 0,
      outstandingByPath: new Map([["src/a.py", 2]]),
      onActiveTarget: vi.fn(),
      onDraft: vi.fn(),
      onCreate: vi.fn(),
      onSetAnnotationState: vi.fn(),
      onMark: vi.fn(),
      onComment: vi.fn(),
      onContext: vi.fn(),
      onBulkReview: vi.fn(),
      onLoadMore: vi.fn(),
    };
    const { rerender } = render(<ReviewStream {...common} activeTargetId="target:a" />);
    const firstProps = pierreReact.diffProps.get("src/a.py");
    const firstOptions = firstProps?.options;
    expect(firstOptions).toBeTruthy();

    rerender(<ReviewStream {...common} activeTargetId="target:b" />);

    const secondProps = pierreReact.diffProps.get("src/a.py");
    const secondOptions = secondProps?.options;
    expect(secondOptions).toBe(firstOptions);
    expect(secondOptions.onGutterUtilityClick).toBe(firstOptions.onGutterUtilityClick);
    expect(secondProps.fileDiff).toBe(firstProps.fileDiff);
  });

  it("reattaches a recovered draft only after a new human line selection", () => {
    const targets = [target("a", "src/a.py", 1)];
    const onDraft = vi.fn();
    render(
      <ReviewStream
        {...streamProps({ "src/a.py": detail("src/a.py") }, [["src/a.py", 1]], "target:a")}
        targets={targets}
        draft={{
          path: "src/old.py",
          startLine: 0,
          endLine: 0,
          side: "additions",
          body: "Preserve this correction",
          kind: "request_change",
          markTarget: true,
          recovered: true,
        }}
        onDraft={onDraft}
      />,
    );
    const options = pierreReact.diffProps.get("src/a.py")?.options;
    expect(options).toBeTruthy();
    options.onGutterUtilityClick({ start: 3, end: 4, side: "additions" });
    expect(onDraft).toHaveBeenCalledWith(expect.objectContaining({
      path: "src/a.py",
      startLine: 3,
      endLine: 4,
      body: "Preserve this correction",
      kind: "request_change",
      markTarget: true,
      recovered: false,
    }));
  });

  it("updates the active target from manual scroll position", () => {
    const targets = [
      target("a_one", "src/a.py", 1),
      target("a_two", "src/a.py", 2),
      target("b_one", "src/b.py", 1),
    ];
    const onActiveTarget = vi.fn();
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 1;
    });
    render(
      <ReviewStream
        targets={targets}
        details={{ "src/a.py": detail("src/a.py"), "src/b.py": detail("src/b.py") }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="split"
        activeTargetId="target:a_one"
        busy={false}
        remainingFileCount={0}
        outstandingByPath={new Map([["src/a.py", 2], ["src/b.py", 1]])}
        onActiveTarget={onActiveTarget}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );

    screen.getByTestId("review-stream").getBoundingClientRect = () => ({ top: 0 } as DOMRect);
    const positions = new Map([
      ["target:a_one", -100],
      ["target:a_two", 100],
      ["target:b_one", 260],
    ]);
    for (const node of document.querySelectorAll<HTMLElement>("[data-review-target-id]")) {
      const top = positions.get(node.dataset.reviewTargetId ?? "") ?? 1000;
      node.getBoundingClientRect = () => ({ top } as DOMRect);
    }

    act(() => {
      screen.getByTestId("review-stream").dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    expect(onActiveTarget).toHaveBeenCalledWith("target:a_two");
    raf.mockRestore();
  });

  it("renders previewable binary media with Preview / Compare / Source instead of a binary refusal", async () => {
    const mediaTarget = target("media", "docs/demo.gif", 0);
    const media: FileDetail = {
      path: "docs/demo.gif",
      status: "modified",
      patch: "",
      renderable: true,
      refusal: "",
      detail: "",
      preview: { kind: "media", media_type: "image/gif", media_kind: "image" },
      degraded: [],
    };
    render(
      <ReviewStream
        targets={[mediaTarget]}
        details={{ "docs/demo.gif": media }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="unified"
        activeTargetId="target:media"
        busy={false}
        remainingFileCount={0}
        outstandingByPath={new Map([["docs/demo.gif", 1]])}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );

    expect(screen.getByTestId("media-stream:docs/demo.gif")).toBeTruthy();
    expect(screen.getByTestId("mock-media-preview:docs/demo.gif").textContent).toBe("preview");
    await userEvent.click(screen.getByRole("button", { name: "compare" }));
    expect(screen.getByTestId("mock-media-preview:docs/demo.gif").textContent).toBe("compare");
    await userEvent.click(screen.getByRole("button", { name: "source" }));
    expect(screen.getByTestId("mock-media-preview:docs/demo.gif").textContent).toBe("source");
    expect(screen.queryByText(/packet carries no text/i)).toBeNull();
  });

  it("keeps an unrenderable file in the stream as an explicit refusal", () => {
    const fallback = target("binary", "logo.png", 0);
    const binary: FileDetail = {
      path: "logo.png",
      patch: "",
      renderable: false,
      refusal: "binary_file",
      detail: "Binary file — no text to review",
      degraded: [],
    };
    render(
      <ReviewStream
        targets={[fallback]}
        details={{ "logo.png": binary }}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="unified"
        activeTargetId="target:binary"
        busy={false}
        remainingFileCount={0}
        outstandingByPath={new Map([["logo.png", 1]])}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );
    expect(screen.getByTestId("code-item:logo.png")).toBeTruthy();
  });

  it("hands only loaded files to CodeView for a 500-file / 1,500-target review", () => {
    const targets = Array.from({ length: 1500 }, (_, index) =>
      target(`large_${index}`, `src/large-${Math.floor(index / 3)}.py`, (index % 3) + 1),
    );
    const details = Object.fromEntries(
      Array.from({ length: 5 }, (_, index) => [`src/large-${index}.py`, detail(`src/large-${index}.py`)]),
    );

    render(
      <ReviewStream
        targets={targets}
        details={details}
        errors={{}}
        annotations={[]}
        draft={null}
        diffStyle="split"
        activeTargetId="target:large_0"
        busy={false}
        remainingFileCount={495}
        readyHiddenFileCount={30}
        outstandingByPath={new Map(Array.from({ length: 500 }, (_, index) => [`src/large-${index}.py`, 3]))}
        onActiveTarget={vi.fn()}
        onDraft={vi.fn()}
        onCreate={vi.fn()}
        onSetAnnotationState={vi.fn()}
        onMark={vi.fn()}
        onComment={vi.fn()}
        onContext={vi.fn()}
        onBulkReview={vi.fn()}
        onLoadMore={vi.fn()}
      />,
    );

    expect(screen.getAllByTestId(/^code-item:/)).toHaveLength(5);
    expect(screen.queryByTestId("code-item:src/large-5.py")).toBeNull();
    expect(screen.getByRole("button", { name: "Show 30 ready files · 495 remaining" })).toBeTruthy();
  });

  it("distinguishes background preparation from ready pagination", async () => {
    const onLoadMore = vi.fn();
    const common = {
      targets: [target("a", "a.py", 1)],
      details: { "a.py": detail("a.py") },
      errors: {},
      annotations: [],
      draft: null,
      diffStyle: "split" as const,
      activeTargetId: "target:a",
      busy: false,
      remainingFileCount: 7,
      outstandingByPath: new Map([["a.py", 1]]),
      onActiveTarget: vi.fn(),
      onDraft: vi.fn(),
      onCreate: vi.fn(),
      onSetAnnotationState: vi.fn(),
      onMark: vi.fn(),
      onComment: vi.fn(),
      onContext: vi.fn(),
      onBulkReview: vi.fn(),
      onLoadMore,
    };
    const { rerender } = render(
      <ReviewStream {...common} preparingHiddenFileCount={7} />,
    );

    expect(screen.getByText("Preparing next 7 files…")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /ready files/ })).toBeNull();

    rerender(<ReviewStream {...common} readyHiddenFileCount={3} preparingHiddenFileCount={4} />);
    await userEvent.click(screen.getByRole("button", { name: "Show 3 ready files · 7 remaining" }));
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });
});
