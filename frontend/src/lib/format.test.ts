import { describe, expect, it } from "vitest";

import { fmtAbsolute, fmtPct, fmtTok, fmtUsd, fmtUsdRate, fmtWhen } from "./format";

describe("fmtTok", () => {
  it("abbreviates token counts and shows a dash for a count the server left out", () => {
    expect(fmtTok(950)).toBe("950");
    expect(fmtTok(12_300)).toBe("12.3k");
    expect(fmtTok(2_500_000)).toBe("2.50M");
    expect(fmtTok(undefined as unknown as number)).toBe("—");
    expect(fmtTok(Number.NaN)).toBe("—");
  });

  it("gives money and percentages the same dash for a missing value", () => {
    expect(fmtUsd(undefined as unknown as number)).toBe("—");
    expect(fmtUsdRate(null as unknown as number)).toBe("—");
    expect(fmtPct(undefined as unknown as number)).toBe("—");
    expect(fmtPct(40)).toBe("40.0%");
  });
});

// Thursday 19 Nov 2026, 15:00 local time.
const NOW = new Date(2026, 10, 19, 15, 0, 0);
const at = (day: number, hour: number, minute: number, month = 10, year = 2026) => new Date(year, month, day, hour, minute, 42);

describe("fmtWhen", () => {
  it("is relative inside a week, local time, no seconds", () => {
    expect(fmtWhen(at(19, 14, 32), NOW)).toBe("today 14:32");
    expect(fmtWhen(at(18, 9, 5), NOW)).toBe("yesterday 09:05");
    expect(fmtWhen(at(20, 9, 0), NOW)).toBe("tomorrow 09:00");
    expect(fmtWhen(at(16, 9, 0), NOW)).toBe("Mon 09:00");
    expect(fmtWhen(at(13, 9, 0), NOW)).toBe("Fri 09:00");
  });

  it("is absolute from seven days on", () => {
    expect(fmtWhen(at(12, 14, 32), NOW)).toBe("12 Nov 2026, 14:32");
    expect(fmtWhen(at(26, 8, 0), NOW)).toBe("26 Nov 2026, 08:00");
    expect(fmtWhen(at(1, 0, 0, 0, 2025), NOW)).toBe("1 Jan 2025, 00:00");
    expect(fmtAbsolute(at(19, 14, 32))).toBe("19 Nov 2026, 14:32");
  });

  it("reads ISO strings, Dates and Unix seconds, and dashes the unreadable", () => {
    const d = at(19, 14, 32);
    expect(fmtWhen(d.toISOString(), NOW)).toBe("today 14:32");
    expect(fmtWhen(Math.floor(d.getTime() / 1000), NOW)).toBe("today 14:32");
    for (const bad of [null, undefined, "", "not a date"]) expect(fmtWhen(bad, NOW)).toBe("—");
  });
});

describe("money", () => {
  it("shows totals and limits with two decimals", () => {
    expect(fmtUsd(0)).toBe("$0.00");
    expect(fmtUsd(0.012)).toBe("$0.01");
    expect(fmtUsd(12.5)).toBe("$12.50");
    expect(fmtUsd(1234.567)).toBe("$1,234.57");
  });

  it("keeps finer precision only for per-unit rates below a dollar", () => {
    expect(fmtUsdRate(0.0123)).toBe("$0.0123");
    expect(fmtUsdRate(0.5)).toBe("$0.50");
    expect(fmtUsdRate(3.14159)).toBe("$3.14");
    expect(fmtUsdRate(0)).toBe("$0.00");
  });
});
