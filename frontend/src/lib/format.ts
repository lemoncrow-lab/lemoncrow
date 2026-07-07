// Shared formatting helpers used across every dashboard page. Consolidates
// the duplicated usdFmt / helpers.fmtUsd / inline toFixed(n) call sites and
// the duplicated fmtDate implementations into one canonical set.

export function fmtUsd(v: number): string {
  // Milli-dollar precision matters for per-call costs, but reads as noise
  // on large totals — switch to a grouped 2-decimal format from $100 up.
  if (Math.abs(v) >= 100)
    return `$${v.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  return `$${v.toFixed(3)}`;
}

export function fmtTok(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}
export function fmtPct(p: number, decimals = 1): string {
  return `${p.toFixed(decimals)}%`;
}

export function parseAt(s: string | null | undefined): Date | null {
  if (!s) return null;
  // ms-epoch integers arrive as numeric strings from OpenCode
  const d = /^\d+$/.test(s) ? new Date(parseInt(s, 10)) : new Date(s);
  return isNaN(d.getTime()) ? null : d;
}

export function fmtDate(s: string | null | undefined): string {
  const d = parseAt(s);
  return d ? d.toLocaleString() : "—";
}

export function fmtDuration(secs: number): string {
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

// Relative time with the absolute timestamp intended for a `title` attr —
// callers should pass `title={fmtDate(value)}` alongside this.
export function fmtRelativeTime(s: string | null | undefined): string {
  const d = parseAt(s);
  if (!d) return "—";
  const diffSec = Math.round((Date.now() - d.getTime()) / 1000);
  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  const diffMonth = Math.round(diffDay / 30);
  if (diffMonth < 12) return `${diffMonth}mo ago`;
  return `${Math.round(diffMonth / 12)}y ago`;
}
