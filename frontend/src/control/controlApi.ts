import { requestBrowserJson } from "../review/reviewApi";

export interface LocalTool {
  name: string;
  description?: string;
}

export interface LocalIntegrationHost {
  id: string;
  label: string;
  description: string;
  detected: boolean;
  configured: boolean;
  status: "configured" | "detected" | "not_detected";
  cached_status?: string | null;
}

export interface LocalIntegrations {
  hosts: LocalIntegrationHost[];
  tools: LocalTool[];
  dispatcher_available: boolean;
  cached_status_available: boolean;
  internal_dispatchable_count: number;
}

export function fetchLocalIntegrations(): Promise<LocalIntegrations> {
  return requestBrowserJson<LocalIntegrations>("/api/local/integrations");
}
