import { fillDailyBuckets, fillHourlyBuckets } from "./Analytics";

describe("Usage timeline windows", () => {
  it("keeps Last 7 days anchored to today when recent daily ingestion is missing", () => {
    const buckets = fillDailyBuckets(
      [
        {
          date: "2026-09-18",
          sessions: 3,
          cost: 1.25,
          input_tokens: 100,
          output_tokens: 20,
        },
      ],
      7,
      new Date("2026-09-23T08:00:00+02:00")
    );

    expect(buckets.map((bucket) => bucket.date)).toEqual([
      "2026-09-17",
      "2026-09-18",
      "2026-09-19",
      "2026-09-20",
      "2026-09-21",
      "2026-09-22",
      "2026-09-23",
    ]);
    expect(buckets.find((bucket) => bucket.date === "2026-09-18")?.sessions).toBe(3);
    expect(buckets.find((bucket) => bucket.date === "2026-09-23")?.sessions).toBe(0);
  });

  it("keeps hourly windows anchored to the current hour instead of the newest data hour", () => {
    const buckets = fillHourlyBuckets(
      [
        {
          date: "2026-09-22 06:00",
          sessions: 2,
          cost: 0.5,
          input_tokens: 50,
          output_tokens: 10,
        },
      ],
      1,
      new Date("2026-09-23T06:42:00Z")
    );

    expect(buckets).toHaveLength(24);
    expect(buckets.at(-1)?.date).toBe("2026-09-23 06:00");
    expect(buckets.at(-1)?.sessions).toBe(0);
  });
});
