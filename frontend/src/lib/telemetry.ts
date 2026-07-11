import posthog from "posthog-js";
import { getTelemetryConfig, postLocalTelemetryEvent } from "./insightsApi";

let initialized = false;

export async function initTelemetry() {
  if (initialized) return;
  initialized = true;
  try {
    const cfg = await getTelemetryConfig();
    const sessionId = newSessionId();
    await postLocalTelemetryEvent("session_start", {
      agent_host: "frontend",
      lemoncrow_version: cfg.service_version,
      os: "browser",
      py_version: "n/a",
      anon_id: cfg.anon_id,
      session_id: sessionId,
    });
    if (!cfg.remote_enabled || !cfg.posthog_key) return;
    posthog.init(cfg.posthog_key, {
      api_host: cfg.posthog_host,
      autocapture: true,
      capture_pageview: true,
      persistence: "localStorage",
      mask_all_text: true,
      mask_all_element_attributes: true,
      sanitize_properties: (props: Record<string, unknown>) =>
        scrubFrontend(props),
    } as any);
    posthog.identify(cfg.anon_id);
  } catch {
    initialized = false;
  }
}

export function scrubFrontend(
  props: Record<string, unknown>
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(props).map(([key, value]) => [key, scrubValue(value)])
  );
}

function scrubValue(value: unknown): unknown {
  if (typeof value === "string") return scrubString(value);
  if (Array.isArray(value)) return value.map(scrubValue);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [
        key,
        scrubValue(item),
      ])
    );
  }
  return value;
}

function scrubString(value: string): string {
  return value
    .replace(
      /(?:git@(?:github|gitlab|bitbucket)\.com:[^\s]+|ssh:\/\/git@[^\s]+|https?:\/\/(?:[^\s/@]+@)?(?:www\.)?(?:github|gitlab|bitbucket)\.[^\s]+)/gi,
      "<repo>"
    )
    .replace(
      /\b(?:sk-[A-Za-z0-9_-]{12,}|gh[opsu]_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b/g,
      "<secret>"
    )
    .replace(/\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g, "<email>")
    .replace(/\b(?:\d{1,3}\.){3}\d{1,3}\b/g, "<ip>")
    .replace(
      /(?<![\w.-])\/(?:Users|home|var|tmp|private|Volumes)\/[^\s,;:'\"]+/g,
      "<path>"
    )
    .replace(/\b[A-Za-z]:\\(?:[^\\\s,;:'\"]+\\?)+/g, "<path>");
}

function newSessionId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `frontend-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
