import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

export type ProductId = "core" | "review" | "runtime";
export type ProductMode = "local" | "hosted" | "hybrid" | "none";

export interface ProductState {
  /** The product artifact/service exists in this deployment. */
  installed: boolean;
  /** The signed-in account/license is allowed to use the product. */
  entitled: boolean;
  /** Product-specific setup needed for normal use is complete. */
  configured: boolean;
  /** This browser session can actually use the product now. */
  available: boolean;
  mode: ProductMode;
  version?: string;
}

export interface RuntimeCapabilities {
  /** The browser is attached to the same developer machine as the runtime. */
  localMachine: boolean;
  /** Detect and configure coding-agent hosts installed on that machine. */
  localIntegrations: boolean;
  /** Open source files in a desktop editor on that machine. */
  localEditor: boolean;
  /** Read live developer-session activity and uncommitted worktree changes. */
  liveWorkspace: boolean;
  /** Start a Review from developer-local uncommitted changes. */
  localReviewCapture: boolean;
  /** Ask the local code surface's model-backed assistant. */
  codeAsk: boolean;
  /** Launch and control local swarm/workflow processes. */
  swarms: boolean;
  /** Inspect host-local agents, skills, MCP tools and projection internals. */
  localDiagnostics: boolean;
  /**
   * The full dashboard intelligence surface is served. A lightweight local
   * Dashboard can still expose Code map, Ask, editor handoff and Review capture
   * independently; absent means the full surface is served.
   */
  fullDashboard?: boolean;
  /**
   * The deterministic code map (files, symbols, search, source, changes and
   * import-level relationships) is served even where the full dashboard is not.
   */
  codeMap?: boolean;
  /**
   * Project Knowledge (repository history context) is served by the local
   * Review service. Capability-advertised only: no hosted route exists yet.
   */
  projectKnowledge?: boolean;
  /**
   * The full-index Knowledge sections (Blocks, Failures, Plans, Rubrics, Memory)
   * are served. Absent means they are served wherever the full dashboard is; a
   * hosted deployment sets false because it answers none of their endpoints.
   */
  knowledge?: boolean;
}

export type ProductFeature =
  | "core.home"
  | "core.settings"
  | "core.usage"
  | "review.directory"
  | "review.capture"
  | "review.compare"
  | "review.ask"
  | "review.verification"
  | "review.map"
  | "runtime.code"
  | "runtime.code_map"
  | "runtime.runs"
  | "runtime.integrations"
  | "runtime.local_editor"
  | "runtime.live_workspace"
  | "runtime.code_ask"
  | "runtime.swarms"
  | "runtime.diagnostics"
  | "runtime.project_knowledge"
  | "runtime.knowledge";

export interface LemonCrowCapabilities {
  schemaVersion: 1;
  products: Record<ProductId, ProductState>;
  features: Record<ProductFeature, boolean>;
  /** Existing fine-grained Runtime flags; retained as the Runtime implementation contract. */
  runtime: RuntimeCapabilities;
}

export const LOCAL_RUNTIME_CAPABILITIES: RuntimeCapabilities = {
  localMachine: true,
  localIntegrations: true,
  localEditor: true,
  liveWorkspace: true,
  localReviewCapture: true,
  codeAsk: true,
  swarms: true,
  localDiagnostics: true,
  fullDashboard: true,
};

/**
 * A local server's basic Dashboard: sessions, usage, savings, settings and
 * diagnostics, plus the deterministic code map over the Runtime Lite index.
 *
 * Browser-to-editor handoff and Code Ask are local-machine capabilities too;
 * they do not require the full/private dashboard. Precise references, generated
 * module purpose and other full-index features remain behind fullDashboard.
 */
export const LOCAL_DASHBOARD_RUNTIME_CAPABILITIES: RuntimeCapabilities = {
  localMachine: true,
  localIntegrations: true,
  localEditor: true,
  liveWorkspace: true,
  localReviewCapture: true,
  codeAsk: true,
  swarms: false,
  localDiagnostics: true,
  fullDashboard: false,
  codeMap: true,
  projectKnowledge: true,
};

export const NO_LOCAL_RUNTIME_CAPABILITIES: RuntimeCapabilities = {
  localMachine: false,
  localIntegrations: false,
  localEditor: false,
  liveWorkspace: false,
  localReviewCapture: false,
  codeAsk: false,
  swarms: false,
  localDiagnostics: false,
  fullDashboard: false,
};

function productState(available: boolean, mode: ProductMode): ProductState {
  return {
    installed: available,
    entitled: available,
    configured: available,
    available,
    mode: available ? mode : "none",
  };
}

function features(
  review: boolean,
  runtime: boolean,
  runtimeFlags: RuntimeCapabilities,
  reviewAsk = review,
): Record<ProductFeature, boolean> {
  return {
    "core.home": true,
    "core.settings": true,
    "core.usage": review || runtime,
    "review.directory": review,
    "review.capture": review,
    "review.compare": review,
    "review.ask": review && reviewAsk,
    "review.verification": review,
    "review.map": review,
    // Full-index/private Runtime intelligence. Local editor, Ask and Review
    // capture are modeled separately below instead of being smuggled behind this flag.
    "runtime.code": runtime && runtimeFlags.fullDashboard !== false,
    // Deterministic /code views, also served by the local Runtime Lite index.
    "runtime.code_map": runtime && (runtimeFlags.fullDashboard !== false || runtimeFlags.codeMap === true),
    "runtime.knowledge": runtime && runtimeFlags.fullDashboard !== false && runtimeFlags.knowledge !== false,
    "runtime.runs": runtime,
    "runtime.integrations": runtime && runtimeFlags.localIntegrations,
    "runtime.local_editor": runtime && runtimeFlags.localEditor,
    "runtime.live_workspace": runtime && runtimeFlags.liveWorkspace,
    "runtime.code_ask": runtime && runtimeFlags.codeAsk,
    "runtime.swarms": runtime && runtimeFlags.swarms,
    "runtime.diagnostics": runtime && runtimeFlags.localDiagnostics,
    "runtime.project_knowledge": runtime && runtimeFlags.projectKnowledge === true,
  };
}

export function createProductCapabilities({
  review,
  runtime,
  mode,
  runtimeCapabilities = runtime ? LOCAL_RUNTIME_CAPABILITIES : NO_LOCAL_RUNTIME_CAPABILITIES,
}: {
  review: boolean;
  runtime: boolean;
  mode: Exclude<ProductMode, "none">;
  runtimeCapabilities?: RuntimeCapabilities;
}): LemonCrowCapabilities {
  return {
    schemaVersion: 1,
    products: {
      core: productState(true, mode),
      review: productState(review, mode),
      runtime: productState(runtime, mode),
    },
    features: features(review, runtime, runtimeCapabilities),
    runtime: runtimeCapabilities,
  };
}

export const LOCAL_PRODUCT_CAPABILITIES = createProductCapabilities({
  review: true,
  runtime: true,
  mode: "local",
});

export const REVIEW_ONLY_PRODUCT_CAPABILITIES = createProductCapabilities({
  review: true,
  runtime: false,
  mode: "local",
});

export const RUNTIME_ONLY_PRODUCT_CAPABILITIES = createProductCapabilities({
  review: false,
  runtime: true,
  mode: "local",
});

export const LOCAL_DASHBOARD_PRODUCT_CAPABILITIES = createProductCapabilities({
  review: true,
  runtime: true,
  mode: "local",
  runtimeCapabilities: LOCAL_DASHBOARD_RUNTIME_CAPABILITIES,
});

export const CORE_ONLY_PRODUCT_CAPABILITIES = createProductCapabilities({
  review: false,
  runtime: false,
  mode: "local",
});

interface DiscoveryProductState {
  installed?: unknown;
  configured?: unknown;
  available?: unknown;
}

interface ProductDiscoveryDocument {
  deployment?: { mode?: unknown };
  /** `dashboard` is published by local servers that serve the lightweight Dashboard. */
  products?: Partial<Record<ProductId | "dashboard", DiscoveryProductState>>;
  product_capabilities?: {
    review?: { ask?: unknown };
    dashboard?: {
      code_map?: unknown;
      code_ask?: unknown;
      local_editor?: unknown;
      review_capture?: unknown;
      project_knowledge?: unknown;
    };
  };
}

interface ProductAccountDocument {
  products?: unknown;
}

function discoveryMode(document: ProductDiscoveryDocument, fallback: LemonCrowCapabilities): Exclude<ProductMode, "none"> {
  const raw = document.deployment?.mode;
  if (raw === "local") return "local";
  if (typeof raw === "string" && raw.length > 0) return "hosted";
  const fallbackMode = fallback.products.core.mode;
  return fallbackMode === "none" ? "local" : fallbackMode;
}

function discoveredBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

export function parseProductCapabilities(
  document: ProductDiscoveryDocument,
  fallback: LemonCrowCapabilities,
): LemonCrowCapabilities {
  const mode = discoveryMode(document, fallback);
  const products = {} as Record<ProductId, ProductState>;
  for (const product of ["core", "review", "runtime"] as const) {
    const base = fallback.products[product];
    const source = document.products?.[product];
    const installed = discoveredBoolean(source?.installed, base.installed);
    const configured = installed && discoveredBoolean(source?.configured, base.configured);
    const available = installed && discoveredBoolean(source?.available, base.available);
    products[product] = {
      ...base,
      installed,
      configured,
      available,
      mode: installed ? mode : "none",
    };
  }
  const reviewAsk = discoveredBoolean(
    document.product_capabilities?.review?.ask,
    fallback.features["review.ask"],
  );
  // A local server that names its Dashboard serves the lightweight local
  // surface. Every action beyond deterministic reads is capability-advertised:
  // older local servers therefore stay hidden instead of receiving broken controls.
  const dashboardCapabilities = document.product_capabilities?.dashboard;
  const runtimeFlags =
    mode === "local" && document.products?.dashboard !== undefined
      ? {
          ...LOCAL_DASHBOARD_RUNTIME_CAPABILITIES,
          codeMap: dashboardCapabilities?.code_map === true,
          codeAsk: dashboardCapabilities?.code_ask === true,
          localEditor: dashboardCapabilities?.local_editor === true,
          localReviewCapture: dashboardCapabilities?.review_capture === true,
          projectKnowledge: dashboardCapabilities?.project_knowledge === true,
        }
      : fallback.runtime;
  return {
    schemaVersion: 1,
    products,
    features: features(products.review.available, products.runtime.available, runtimeFlags, reviewAsk),
    runtime: runtimeFlags,
  };
}

export function applyProductEntitlements(
  capabilities: LemonCrowCapabilities,
  account: ProductAccountDocument,
): LemonCrowCapabilities {
  if (!Array.isArray(account.products)) return capabilities;
  const granted = new Set(
    account.products.filter((value): value is "review" | "runtime" => value === "review" || value === "runtime"),
  );
  const products: Record<ProductId, ProductState> = { ...capabilities.products };
  for (const product of ["review", "runtime"] as const) {
    const base = capabilities.products[product];
    const entitled = granted.has(product);
    products[product] = { ...base, entitled, available: base.available && entitled };
  }
  return {
    ...capabilities,
    products,
    features: features(
      products.review.available,
      products.runtime.available,
      capabilities.runtime,
      capabilities.features["review.ask"],
    ),
  };
}

interface ProductDiscoveryResult {
  document: ProductDiscoveryDocument | null;
  /** The endpoint exists conceptually but could not be read; distinct from a legacy 404. */
  failed: boolean;
}

let discoveryDocumentRequest: Promise<ProductDiscoveryResult> | null = null;

async function fetchDiscoveryDocument(): Promise<ProductDiscoveryResult> {
  if (discoveryDocumentRequest) return discoveryDocumentRequest;
  discoveryDocumentRequest = (async () => {
    try {
      const response = await fetch("/.well-known/lemoncrow-server.json", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (response.ok) {
        return { document: await response.json() as ProductDiscoveryDocument, failed: false };
      }
      // A missing well-known document is a supported legacy server. Other
      // failures are degraded discovery and should be surfaced without hiding
      // fallback-safe Review controls.
      return { document: null, failed: response.status !== 404 };
    } catch {
      return { document: null, failed: true };
    }
  })();
  return discoveryDocumentRequest;
}

async function fetchProductAccount(): Promise<ProductAccountDocument | null> {
  try {
    const response = await fetch("/v1/account", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    return response.ok ? await response.json() as ProductAccountDocument : null;
  } catch {
    return null;
  }
}

async function discoverProductCapabilities(
  fallback: LemonCrowCapabilities,
): Promise<{ capabilities: LemonCrowCapabilities; discovered: boolean; degraded: boolean }> {
  const discovery = await fetchDiscoveryDocument();
  const discovered = discovery.document ? parseProductCapabilities(discovery.document, fallback) : fallback;
  // Local Review has no account entitlement boundary. Asking a local server for
  // /v1/account creates a noisy 401 during an otherwise healthy anonymous
  // browser session, so only managed/hybrid deployments query account grants.
  const needsAccount = Object.values(discovered.products).some(
    (product) => product.mode === "hosted" || product.mode === "hybrid",
  );
  const account = needsAccount ? await fetchProductAccount() : null;
  return {
    capabilities: account ? applyProductEntitlements(discovered, account) : discovered,
    discovered: discovery.document !== null,
    degraded: discovery.failed,
  };
}

export async function fetchProductCapabilities(
  fallback: LemonCrowCapabilities,
): Promise<LemonCrowCapabilities> {
  return (await discoverProductCapabilities(fallback)).capabilities;
}

/**
 * How settled the manifest in context is. A static provider (and the bundle
 * default) declares it; discovery is "pending" until the serving backend has
 * answered, then "discovered", "fallback" for a legacy server with no manifest,
 * or "degraded" when discovery itself failed and safe defaults are in use.
 */
export type ProductCapabilitiesResolution = "declared" | "pending" | "discovered" | "fallback" | "degraded";

const ProductCapabilitiesContext = createContext<LemonCrowCapabilities>(LOCAL_PRODUCT_CAPABILITIES);
const ProductCapabilitiesResolutionContext = createContext<ProductCapabilitiesResolution>("declared");

export function ProductCapabilitiesProvider({
  value,
  children,
}: {
  value: LemonCrowCapabilities;
  children: ReactNode;
}) {
  return <ProductCapabilitiesContext.Provider value={value}>{children}</ProductCapabilitiesContext.Provider>;
}

export function DiscoveredProductCapabilitiesProvider({
  fallback,
  children,
}: {
  fallback: LemonCrowCapabilities;
  children: ReactNode;
}) {
  // Surfaces keep rendering from the fallback while discovery runs; consumers
  // that must not infer product presence read useResolvedProductCapabilities.
  const [state, setState] = useState<{ value: LemonCrowCapabilities; resolution: ProductCapabilitiesResolution }>(
    () => ({ value: fallback, resolution: "pending" }),
  );
  useEffect(() => {
    let active = true;
    setState((current) => (current.value === fallback && current.resolution === "pending"
      ? current
      : { value: fallback, resolution: "pending" }));
    void discoverProductCapabilities(fallback).then(
      ({ capabilities, discovered, degraded }) => {
        if (active) {
          setState({
            value: capabilities,
            resolution: discovered ? "discovered" : degraded ? "degraded" : "fallback",
          });
        }
      },
      () => {
        // Never leave capability-gated controls silently pending forever.
        if (active) setState({ value: fallback, resolution: "degraded" });
      },
    );
    return () => { active = false; };
  }, [fallback]);
  return (
    <ProductCapabilitiesContext.Provider value={state.value}>
      <ProductCapabilitiesResolutionContext.Provider value={state.resolution}>{children}</ProductCapabilitiesResolutionContext.Provider>
    </ProductCapabilitiesContext.Provider>
  );
}

/** Compatibility provider for callers that only override Runtime locality flags. */
export function RuntimeCapabilitiesProvider({
  value,
  children,
}: {
  value: RuntimeCapabilities;
  children: ReactNode;
}) {
  const base = createProductCapabilities({
    review: true,
    runtime: true,
    mode: value.localMachine ? "local" : "hosted",
    runtimeCapabilities: value,
  });
  return <ProductCapabilitiesContext.Provider value={base}>{children}</ProductCapabilitiesContext.Provider>;
}

export function useProductCapabilities(): LemonCrowCapabilities {
  return useContext(ProductCapabilitiesContext);
}

export function useProductCapabilitiesResolution(): ProductCapabilitiesResolution {
  return useContext(ProductCapabilitiesResolutionContext);
}

/**
 * The manifest once it is settled, or null while discovery is pending. Gate
 * product-owned requests on this, never on the provisional fallback
 * (isolation §25: the UI must not infer product presence).
 */
export function useResolvedProductCapabilities(): LemonCrowCapabilities | null {
  const capabilities = useContext(ProductCapabilitiesContext);
  return useContext(ProductCapabilitiesResolutionContext) === "pending" ? null : capabilities;
}

export function useRuntimeCapabilities(): RuntimeCapabilities {
  return useProductCapabilities().runtime;
}

export function productAvailable(capabilities: LemonCrowCapabilities, product: ProductId): boolean {
  return capabilities.products[product].available;
}
