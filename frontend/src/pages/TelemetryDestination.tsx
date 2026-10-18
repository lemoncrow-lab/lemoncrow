import { useEffect, useState } from "react";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Select, SelectItem } from "../components/ui/select";
import { Chip } from "../components/WorkbenchUI";
import {
  getTelemetryConfig,
  updateTelemetryConfig,
  type TelemetryConfig,
  type TelemetryConfigUpdate,
  type TelemetryDestinationSetting,
} from "../lib/insightsApi";

const OPTIONS: Array<{ value: TelemetryDestinationSetting; label: string }> = [
  { value: "auto", label: "Automatic" },
  { value: "off", label: "Local only" },
  { value: "lemoncrow", label: "LemonCrow cloud" },
  { value: "server", label: "My LemonCrow server" },
  { value: "custom", label: "Custom host" },
];

function optionHelp(
  setting: TelemetryDestinationSetting,
  config: TelemetryConfig
): string {
  switch (setting) {
    case "auto":
      return config.server_hosted
        ? `Follows your LemonCrow server (${config.server_url}).`
        : "Uses lemoncrow.com, or your LemonCrow server once it is hosted.";
    case "off":
      return "Keep everything on this machine.";
    case "lemoncrow":
      return "LemonCrow's hosted service at lemoncrow.com.";
    case "server":
      return config.server_url
        ? `The LemonCrow server this install uses (${config.server_url}).`
        : "The LemonCrow server this install uses.";
    case "custom":
      return "Any host that serves /api/telemetry/rollup and /api/telemetry/events.";
  }
}

export function describeDestination(config: TelemetryConfig): string {
  if (config.destination !== "off") {
    return `Daily anonymous rollups go to ${config.destination_url}.`;
  }
  if (config.destination_error) {
    return `Nothing leaves this machine: ${config.destination_error}.`;
  }
  if (config.destination_source === "env") {
    return "Nothing leaves this machine (DO_NOT_TRACK or LEMONCROW_TELEMETRY=off is set).";
  }
  return "Nothing leaves this machine.";
}

/** Choose where daily anonymous rollups go; read-only under an admin policy. */
export default function TelemetryDestinationPanel() {
  const [config, setConfig] = useState<TelemetryConfig | null>(null);
  const [setting, setSetting] = useState<TelemetryDestinationSetting>("auto");
  const [endpoint, setEndpoint] = useState("");
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const apply = (next: TelemetryConfig) => {
    setConfig(next);
    setSetting(next.destination_setting);
    setEndpoint(next.endpoint);
    setToken("");
  };

  useEffect(() => {
    let active = true;
    getTelemetryConfig()
      .then((next) => {
        if (active) apply(next);
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      active = false;
    };
  }, []);

  if (!config) {
    return error ? (
      <div role="alert" className="text-sm text-red-300">
        {error}
      </div>
    ) : (
      <div className="text-sm text-neutral-400">Loading...</div>
    );
  }

  const locked = config.managed;
  const dirty =
    setting !== config.destination_setting ||
    (setting === "custom" &&
      (endpoint.trim() !== config.endpoint || token !== ""));

  const save = async () => {
    setSaving(true);
    setSaved(false);
    setError(null);
    const payload: TelemetryConfigUpdate = { destination: setting };
    if (setting === "custom") {
      payload.endpoint = endpoint.trim();
      if (token) payload.token = token;
    }
    try {
      apply(await updateTelemetryConfig(payload));
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Select
          aria-label="Telemetry destination"
          value={setting}
          onValueChange={(value) => {
            setSetting(value as TelemetryDestinationSetting);
            setSaved(false);
          }}
          disabled={locked || saving}
          className="min-w-[220px]"
        >
          {OPTIONS.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </Select>
        {locked ? (
          <Chip tone="warning">Managed by your organization</Chip>
        ) : (
          <Button
            type="button"
            variant="default"
            onClick={() => void save()}
            disabled={!dirty || saving}
          >
            {saving ? "Saving..." : "Save"}
          </Button>
        )}
      </div>
      <p className="text-sm text-neutral-400">{optionHelp(setting, config)}</p>
      {setting === "custom" && !locked && (
        <div className="grid gap-3 md:grid-cols-2">
          <label className="space-y-1 text-xs text-neutral-400">
            <span>Base URL</span>
            <Input
              aria-label="Custom telemetry URL"
              placeholder="https://telemetry.example.com"
              value={endpoint}
              onChange={(event) => setEndpoint(event.target.value)}
            />
          </label>
          <label className="space-y-1 text-xs text-neutral-400">
            <span>Bearer token (optional)</span>
            <Input
              aria-label="Custom telemetry token"
              type="password"
              autoComplete="off"
              placeholder={
                config.token_set ? "Saved; leave blank to keep" : "None"
              }
              value={token}
              onChange={(event) => setToken(event.target.value)}
            />
          </label>
        </div>
      )}
      <p className="text-sm text-foreground">{describeDestination(config)}</p>
      {locked && config.policy_path && (
        <p className="text-xs text-neutral-500">Set by {config.policy_path}.</p>
      )}
      {error && (
        <div role="alert" className="text-sm text-red-300">
          {error}
        </div>
      )}
      {saved && !error && (
        <div className="text-xs text-emerald-300">Saved.</div>
      )}
    </div>
  );
}
