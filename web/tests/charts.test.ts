import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import {
  chartNotes,
  exactUnix,
  markersFor,
  parseAnalysis,
  parseBars,
  parseIndicators,
  replayIndex,
} from "@/lib/charts";

describe("trade markers", () => {
  const candles = [{ open_time: 1_700_000_000 }, { open_time: 1_700_000_060 }];

  it("uses the trade-log timestamp and does not snap a nearby time", () => {
    const placed = markersFor(
      candles,
      [{ entry_time: "1700000060", exit_time: "1700000001" }],
      1_700_000_060,
    );
    expect(placed.markers).toEqual([{ time: 1_700_000_060, timeText: "1700000060", kind: "entry" }]);
    expect(placed.considered).toBe(2);
    expect(placed.shown).toBe(1);
    expect(placed.markers.some((marker) => marker.time === 1_700_000_000)).toBe(false);
    expect(placed.markers.some((marker) => marker.time === 1_700_000_001)).toBe(false);
  });

  it("hides a later marker until that bar is revealed", () => {
    const early = markersFor(
      candles,
      [{ entry_time: "1700000000", exit_time: "1700000060" }],
      1_700_000_000,
    );
    expect(early.markers.map((marker) => marker.time)).toEqual([1_700_000_000]);
    const later = markersFor(
      candles,
      [{ entry_time: "1700000000", exit_time: "1700000060" }],
      1_700_000_060,
    );
    expect(later.markers.map((marker) => [marker.kind, marker.time])).toEqual([
      ["entry", 1_700_000_000],
      ["exit", 1_700_000_060],
    ]);
  });

  it("rejects a text timestamp that is not the integer second", () => {
    expect(exactUnix("=cmd")).toBeNull();
    expect(exactUnix("-12")).toBeNull();
    expect(exactUnix("1700000060")).toBe(1_700_000_060);
    expect(exactUnix("01700000060")).toBeNull();
  });
});

describe("point notes", () => {
  it("names invalid points and a shorter window", () => {
    expect(chartNotes(2, 5, 2, 5, "bars shown")).toEqual([
      "2 of 5 points valid",
      "2 of 5 bars shown",
    ]);
    expect(chartNotes(4, 4, 2, 9, "bars shown")).toEqual(["2 of 9 bars shown"]);
    expect(chartNotes(4, 4, 4, 4, "bars shown")).toEqual([]);
    expect(chartNotes(1, 2, 2, 9, "points shown")).toEqual(["1 of 2 points valid", "2 of 9 points shown"]);
  });

  it("steps the replay index inside the candle list", () => {
    expect(replayIndex(-1, 3)).toBe(0);
    expect(replayIndex(0, 3)).toBe(0);
    expect(replayIndex(1, 3)).toBe(1);
    expect(replayIndex(9, 3)).toBe(2);
  });
});

describe("analysis payload", () => {
  it("keeps only the buckets the API sent", () => {
    const parsed = parseAnalysis({
      schema: "tradoortest.backtest_analysis.v1",
      available: true,
      timezone: "UTC",
      trades_total: 4,
      trades_used: 3,
      hours: [
        { hour_utc: 0, count: 2 },
        { hour_utc: 14, count: 1 },
      ],
      weekdays: [{ weekday_utc: 3, weekday: "Thursday", count: 3 }],
    });
    expect(parsed?.available).toBe(true);
    expect(parsed?.hours).toEqual([
      { hourUtc: 0, count: 2 },
      { hourUtc: 14, count: 1 },
    ]);
    expect(parsed?.hours).toHaveLength(2);
    expect(parsed?.weekdays.map((bucket) => bucket.weekday)).toEqual(["Thursday"]);
  });

  it("is unavailable when the API says so, with no filler buckets", () => {
    const parsed = parseAnalysis({
      schema: "tradoortest.backtest_analysis.v1",
      available: false,
      timezone: "UTC",
      trades_total: 0,
      trades_used: 0,
    });
    expect(parsed).toEqual({
      available: false,
      tradesTotal: 0,
      tradesUsed: 0,
      hours: [],
      weekdays: [],
      dropped: 0,
      reason: null,
    });
  });

  it("drops a zero count instead of drawing an empty hour", () => {
    const parsed = parseAnalysis({
      schema: "tradoortest.backtest_analysis.v1",
      available: true,
      trades_total: 1,
      trades_used: 1,
      hours: [
        { hour_utc: 0, count: 0 },
        { hour_utc: 5, count: 1 },
      ],
      weekdays: [{ weekday_utc: 0, weekday: "Monday", count: 1 }],
    });
    expect(parsed?.hours).toEqual([{ hourUtc: 5, count: 1 }]);
    expect(parsed?.dropped).toBe(1);
  });
});

describe("bar payload", () => {
  it("keeps the server spelling and counts a candle that is not a number", () => {
    const parsed = parseBars({
      schema: "tradoortest.backtest_bars.v1",
      available: true,
      bar_size: "1m",
      slots_total: 2,
      slots_shown: 2,
      assets: [
        {
          asset_id: "sol",
          total: 2,
          shown: 2,
          rejected: 0,
          bars: [
            {
              open_time: 1_700_000_000,
              open: "10",
              high: "11",
              low: "9",
              close: "-12.34",
              volume: "1",
              synthetic: false,
            },
            {
              open_time: 1_700_000_060,
              open: "10",
              high: "11",
              low: "9",
              close: "nope",
              volume: "1",
              synthetic: false,
            },
          ],
        },
      ],
    });
    expect(parsed?.available).toBe(true);
    if (!parsed || !parsed.available) {
      return;
    }
    expect(parsed.assets[0]?.bars).toEqual([
      {
        open_time: 1_700_000_000,
        open: "10",
        high: "11",
        low: "9",
        close: "-12.34",
        volume: "1",
        synthetic: false,
      },
    ]);
    expect(parsed.assets[0]?.dropped).toBe(1);
    expect(chartNotes(1, 2, 2, 2, "bars shown")).toEqual(["1 of 2 points valid"]);
  });

  it("drops hex, exponent, blank, and non-finite spellings on bars and indicators", () => {
    const junk = ["0x10", "1e10", " ", "", "NaN", "Infinity"];
    const bars = parseBars({
      schema: "tradoortest.backtest_bars.v1",
      available: true,
      bar_size: "1m",
      slots_total: 8,
      slots_shown: 8,
      assets: [
        {
          asset_id: "sol",
          total: 8,
          shown: 8,
          rejected: 0,
          bars: [
            {
              open_time: 1_700_000_000,
              open: "10",
              high: "11",
              low: "9",
              close: "10",
              volume: "1",
              synthetic: false,
            },
            {
              open_time: 1_700_000_060,
              open: 12,
              high: 13,
              low: 11,
              close: 12,
              volume: 1,
              synthetic: false,
            },
            ...junk.map((close, index) => ({
              open_time: 1_700_000_120 + index * 60,
              open: "10",
              high: "11",
              low: "9",
              close,
              volume: "1",
              synthetic: false,
            })),
          ],
        },
      ],
    });
    const indicators = parseIndicators({
      schema: "tradoortest.backtest_indicators.v1",
      available: true,
      slots_total: 8,
      slots_shown: 8,
      series: [
        {
          id: "sma_2",
          asset_id: "sol",
          indicator: "sma",
          output: null,
          total: 8,
          shown: 8,
          valid: 2,
          rejected: 0,
          points: [
            { open_time: 1_700_000_000, value: "15.0" },
            { open_time: 1_700_000_060, value: 16 },
            ...junk.map((value, index) => ({
              open_time: 1_700_000_120 + index * 60,
              value,
            })),
          ],
        },
      ],
    });
    expect(bars?.available).toBe(true);
    expect(indicators?.available).toBe(true);
    if (!bars?.available || !indicators?.available) {
      return;
    }
    expect(bars.assets[0]?.bars.map((bar) => bar.close)).toEqual(["10", "12"]);
    expect(bars.assets[0]?.dropped).toBe(junk.length);
    expect(indicators.series[0]?.points.map((point) => point.valueText)).toEqual(["15.0", "16"]);
    expect(indicators.series[0]?.dropped).toBe(junk.length);
    const kept = JSON.stringify({ bars, indicators });
    for (const bad of ["0x10", "1e10", "NaN", "Infinity"]) {
      expect(kept).not.toContain(bad);
    }
    expect(chartNotes(2, 2 + junk.length, 8, 8, "bars shown")).toEqual(["2 of 8 points valid"]);
  });
});

describe("lightweight-charts is a pinned client import", () => {
  it("pins an exact version and does not load the library from a CDN", () => {
    const root = path.resolve(import.meta.dirname, "..");
    const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8")) as {
      dependencies: Record<string, string>;
    };
    expect(pkg.dependencies["lightweight-charts"]).toBe("5.3.0");
    expect(pkg.dependencies["lightweight-charts"]).not.toMatch(/[\^~]/);
    const lock = readFileSync(path.join(root, "package-lock.json"), "utf8");
    expect(lock).toContain('"node_modules/lightweight-charts"');
    expect(lock).toContain('"version": "5.3.0"');
    const source = readFileSync(path.join(root, "components/price-chart.tsx"), "utf8");
    expect(source).toContain('import("lightweight-charts")');
    expect(source).not.toContain("attributionLogo");
    expect(source).not.toContain("unpkg");
    expect(source).not.toContain("jsdelivr");
    expect(source).not.toMatch(/from ["']lightweight-charts["']/);
    const css = readFileSync(path.join(root, "app/globals.css"), "utf8");
    expect(css).not.toContain("tv-attr-logo");
  });
});
