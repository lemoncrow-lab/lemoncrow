// Shared formatting helpers used across every dashboard page. Consolidates
// the duplicated usdFmt / helpers.fmtUsd / inline toFixed(n) call sites and
// the duplicated fmtDate implementations into one canonical set.

/** A number the server left out (or sent as null) renders as a dash instead of taking the page down. */
function missing(v: unknown): boolean {
  return typeof v !== "number" || !Number.isFinite(v);
}

/** Totals and limits: always two decimals, grouped ("$1,234.50", "$0.40"). */
export function fmtUsd(v: number): string {
  if (missing(v)) return "—";
  return `$${v.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/**
 * Per-unit rates (one call, one task, one token price) where cents round to zero:
 * up to four decimals below $1, two at or above it.
 */
export function fmtUsdRate(v: number): string {
  if (missing(v) || Math.abs(v) >= 1 || v === 0) return fmtUsd(v);
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
}

export function fmtTok(n: number): string {
  // A field the server left out must not take the page down (Savings once read a misspelt key).
  if (missing(n)) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toLocaleString();
}
export function fmtPct(p: number, decimals = 1): string {
  if (missing(p)) return "—";
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
  return d ? fmtAbsolute(d) : "—";
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;

const pad = (n: number) => String(n).padStart(2, "0");
const clock = (d: Date) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;

/** "19 Nov 2026, 14:32" in local time, no seconds. */
export function fmtAbsolute(d: Date): string {
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${clock(d)}`;
}

/** A Date from an ISO/ms string, a Date, or Unix **seconds** (the hosted API's unit). */
function toDate(value: Date | string | number | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") return Number.isFinite(value) ? new Date(value * 1000) : null;
  return parseAt(value);
}

/**
 * The one date format for timestamps people read. Local time, no seconds.
 * Within 7 calendar days either way: "today 14:32", "yesterday 14:32", "tomorrow 09:00", "Mon 09:00".
 * Further away: "19 Nov 2026, 14:32". Numbers are Unix seconds. Empty or invalid input gives "—".
 */
export function fmtWhen(value: Date | string | number | null | undefined, now: Date = new Date()): string {
  const d = toDate(value);
  if (!d) return "—";
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(d) - startOf(now)) / 86_400_000);
  if (days === 0) return `today ${clock(d)}`;
  if (days === -1) return `yesterday ${clock(d)}`;
  if (days === 1) return `tomorrow ${clock(d)}`;
  if (Math.abs(days) < 7) return `${WEEKDAYS[d.getDay()]} ${clock(d)}`;
  return fmtAbsolute(d);
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
