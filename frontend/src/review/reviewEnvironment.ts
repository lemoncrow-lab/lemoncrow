import { useEffect, useState } from "react";

export type ReviewDeploymentMode = "local" | "customer_hosted" | "managed_dedicated" | "managed_shared";

export interface ReviewProductCapabilities {
  local_workspace: boolean;
  workspace_refresh: boolean;
  cross_device_history: boolean;
  collaboration: boolean;
  participants: boolean;
  requests: boolean;
  provider_sync: boolean;
  organization_policy: boolean;
  sharing: boolean;
  guest_review: boolean;
  private_drafts: boolean;
}

export interface ReviewEnvironment {
  mode: ReviewDeploymentMode;
  review: ReviewProductCapabilities;
  discovered: boolean;
}

interface DiscoveryDocument {
  deployment?: { mode?: unknown };
  product_capabilities?: { review?: Record<string, unknown> };
}

const MODES = new Set<ReviewDeploymentMode>([
  "local",
  "customer_hosted",
  "managed_dedicated",
  "managed_shared",
]);

function fallbackCapabilities(mode: ReviewDeploymentMode): ReviewProductCapabilities {
  const local = mode === "local";
  return {
    local_workspace: local,
    workspace_refresh: local,
    cross_device_history: false,
    collaboration: false,
    participants: false,
    requests: false,
    provider_sync: false,
    organization_policy: false,
    sharing: false,
    guest_review: false,
    private_drafts: false,
  };
}

function bool(source: Record<string, unknown> | undefined, key: keyof ReviewProductCapabilities, fallback: boolean): boolean {
  return typeof source?.[key] === "boolean" ? source[key] as boolean : fallback;
}

export function fallbackReviewEnvironment(mode: ReviewDeploymentMode): ReviewEnvironment {
  return { mode, review: fallbackCapabilities(mode), discovered: false };
}

export function parseReviewEnvironment(
  document: DiscoveryDocument,
  fallbackMode: ReviewDeploymentMode,
): ReviewEnvironment {
  const rawMode = document.deployment?.mode;
  const mode = typeof rawMode === "string" && MODES.has(rawMode as ReviewDeploymentMode)
    ? rawMode as ReviewDeploymentMode
    : fallbackMode;
  const fallback = fallbackCapabilities(mode);
  const source = document.product_capabilities?.review;
  return {
    mode,
    discovered: typeof rawMode === "string" && MODES.has(rawMode as ReviewDeploymentMode),
    review: {
      local_workspace: bool(source, "local_workspace", fallback.local_workspace),
      workspace_refresh: bool(source, "workspace_refresh", fallback.workspace_refresh),
      cross_device_history: bool(source, "cross_device_history", fallback.cross_device_history),
      collaboration: bool(source, "collaboration", fallback.collaboration),
      participants: bool(source, "participants", fallback.participants),
      requests: bool(source, "requests", fallback.requests),
      provider_sync: bool(source, "provider_sync", fallback.provider_sync),
      organization_policy: bool(source, "organization_policy", fallback.organization_policy),
      sharing: bool(source, "sharing", fallback.sharing),
      guest_review: bool(source, "guest_review", fallback.guest_review),
      private_drafts: bool(source, "private_drafts", fallback.private_drafts),
    },
  };
}

const environmentRequests = new Map<ReviewDeploymentMode, Promise<ReviewEnvironment>>();

export function fetchReviewEnvironment(
  fallbackMode: ReviewDeploymentMode = "local",
): Promise<ReviewEnvironment> {
  const cached = environmentRequests.get(fallbackMode);
  if (cached) return cached;
  const request = (async () => {
    try {
      const response = await fetch("/.well-known/lemoncrow-server.json", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return fallbackReviewEnvironment(fallbackMode);
      return parseReviewEnvironment(await response.json() as DiscoveryDocument, fallbackMode);
    } catch {
      return fallbackReviewEnvironment(fallbackMode);
    }
  })();
  environmentRequests.set(fallbackMode, request);
  return request;
}

export function useReviewEnvironment(
  fallbackMode: ReviewDeploymentMode = "local",
): ReviewEnvironment {
  const [environment, setEnvironment] = useState<ReviewEnvironment>(() => fallbackReviewEnvironment(fallbackMode));
  useEffect(() => {
    let active = true;
    void fetchReviewEnvironment(fallbackMode).then((next) => {
      if (active) setEnvironment(next);
    });
    return () => { active = false; };
  }, [fallbackMode]);
  return environment;
}

export function reviewEnvironmentLabel(mode: ReviewDeploymentMode): string {
  if (mode === "local") return "Local";
  if (mode === "customer_hosted") return "Customer hosted";
  return "Hosted";
}

export function reviewEnvironmentDescription(environment: ReviewEnvironment): string {
  if (environment.mode === "local") {
    return "Stored on this machine; Review can refresh directly from your local workspace.";
  }
  if (environment.mode === "customer_hosted") {
    return "Shared Review hosted by your organization with durable collaboration history.";
  }
  if (environment.mode === "managed_shared") {
    return "Shared managed Review with durable collaboration history.";
  }
  return "Dedicated managed Review with durable collaboration history.";
}
