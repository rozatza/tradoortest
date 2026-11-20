import { describe, expect, it } from "vitest";

import { exactUnix, markersFor, parseBars } from "@/lib/charts";

const candle = (time: number) => ({ open_time: time });

describe("CR17 markersFor", () => {
  it("rejects exponent, leading-zero, decimal, padded, and hex trade times", () => {
    const candles = [candle(1_000_000_000), candle(1_700_000_000)];
    const trades = [
      { entry_time: "1e9" },
      { entry_time: "01700000000" },
      { entry_time: "1700000000.0" },
      { entry_time: " 1700000000" },
      { entry_time: "0x6553F100" },
    ];
    const placed = markersFor(candles, trades, 2_000_000_000);
    expect(placed.shown).toBe(0);
    expect(placed.considered).toBe(5);
  });

  it("caps markers at 500 and counts the rest", () => {
    const candles = Array.from({ length: 600 }, (_, index) => candle(1_700_000_000 + index * 60));
    const trades = candles.map((item) => ({ entry_time: String(item.open_time) }));
    const placed = markersFor(candles, trades, 1_800_000_000);
    expect(placed.shown).toBe(500);
    expect(placed.considered).toBe(600);
  });
});

describe("CR17 parseBars", () => {
  const row = (time: number, value = "1.5") => ({
    open_time: time,
    open: value,
    high: value,
    low: value,
    close: value,
    volume: "1",
    synthetic: false,
  });
  const doc = (bars: unknown[]) => ({
    schema: "tradoortest.backtest_bars.v1",
    available: true,
    bar_size: "1m",
    slots_total: 3,
    slots_shown: 3,
    assets: [{ asset_id: "sol", total: 3, shown: 3, rejected: 0, bars }],
  });

  it("counts a duplicate open_time as dropped", () => {
    const parsed = parseBars(doc([row(60), row(60, "2.5"), row(120)]));
    if (!parsed || !parsed.available) {
      throw new Error("parse");
    }
    expect(parsed.assets[0]?.bars.map((bar) => bar.open_time)).toEqual([60, 120]);
    expect(parsed.assets[0]?.dropped).toBe(1);
  });

  it("rejects a negative or fractional open_time", () => {
    expect(exactUnix(-60)).toBeNull();
    expect(exactUnix(60.5)).toBeNull();
    const parsed = parseBars(doc([row(-60), row(60)]));
    if (!parsed || !parsed.available) {
      throw new Error("parse");
    }
    expect(parsed.assets[0]?.bars.map((bar) => bar.open_time)).toEqual([60]);
  });
});
