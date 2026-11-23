import { useOnline } from "./useOnline";

/** SPEC 4.20 Offline: the browser's own online flag; the server still decides what it accepts. */
export const OFFLINE_BANNER_TEXT = "Offline · marks and notes need the connection";

export function FlowOfflineBanner() {
  const online = useOnline();
  if (online) return null;
  return (
    <p role="status" data-testid="flow-offline-banner" className="border-b border-p1-line bg-p1-bg px-4 py-2 text-label">
      {OFFLINE_BANNER_TEXT}
    </p>
  );
}
