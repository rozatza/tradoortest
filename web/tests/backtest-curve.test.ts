import { describe, expect, it } from "vitest";

import {
  DOWNLOADS,
  EQUITY_DRAW_CAP,
  METRIC_FACTS,
  METRIC_FIELDS,
  TRADE_COLUMNS,
  TRADE_PAGE_SIZE,
  curveCoordinates,
  isEquityPoint,
  pointsForCurve,
  tradePage,
  type EquityPoint,
} from "@/lib/backtest";

function rng(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 2 ** 32;
  };
}

function dec(value: number): string {
  return (Math.round(value * 100) / 100).toFixed(2);
}

function series(n: number, seed: number, mode: string): EquityPoint[] {
  const next = rng(seed);
  let equity = 10000;
  const out: EquityPoint[] = [];
  for (let index = 0; index < n; index += 1) {
    if (mode === "walk") {
      equity = Math.max(0.01, equity * (1 + (next() - 0.5) * 0.02));
    } else if (mode === "flat") {
      equity = 100;
    } else if (mode === "ties") {
      equity = 100 + Math.floor(next() * 3);
    } else if (mode === "spike") {
      if (index === Math.floor(n / 2)) {
        equity = 1e9;
      } else if (index === Math.floor(n / 3)) {
        equity = 0.01;
      } else {
        equity = 100 + next();
      }
    } else if (mode === "neg") {
      equity = (next() - 0.5) * 1000;
    } else if (mode === "mono") {
      equity = 100 + index;
    }
    out.push({ open_time: 1000 + index * 60, equity: dec(equity), cash: "0" });
  }
  return out;
}

function byNumber(left: string | number, right: string | number): number {
  return Number(left) - Number(right);
}

describe("equity downsampling keeps real extremes", () => {
  for (const mode of ["walk", "flat", "ties", "spike", "neg", "mono"]) {
    for (const n of [1, 2, 3, 499, 500, 501, 502, 997, 1000, 1001, 5000, 20000]) {
      it(`${mode} n=${n}`, () => {
        const pts = series(n, n * 7 + mode.length, mode);
        const out = pointsForCurve(pts, EQUITY_DRAW_CAP);
        expect(out.length).toBeLessThanOrEqual(EQUITY_DRAW_CAP);
        expect(out[0]).toBe(pts[0]);
        expect(out[out.length - 1]).toBe(pts[pts.length - 1]);
        const set = new Set(pts);
        for (const point of out) {
          expect(set.has(point)).toBe(true);
        }
        const idx = out.map((point) => pts.indexOf(point));
        for (let index = 1; index < idx.length; index += 1) {
          expect(idx[index]).toBeGreaterThan(idx[index - 1]!);
        }
        const all = pts.map((point) => point.equity).sort(byNumber);
        const drawn = out.map((point) => point.equity).sort(byNumber);
        expect(drawn[0]).toBe(all[0]);
        expect(drawn[drawn.length - 1]).toBe(all[all.length - 1]);
      });
    }
  }

  it("cap edge cases", () => {
    const pts = series(100, 3, "walk");
    expect(pointsForCurve(pts, 0)).toEqual([]);
    expect(pointsForCurve(pts, 1)).toEqual([pts[0]]);
    for (const cap of [2, 3, 4, 5, 10]) {
      const out = pointsForCurve(pts, cap);
      expect(out.length).toBeLessThanOrEqual(cap);
      expect(out[0]).toBe(pts[0]);
      expect(out[out.length - 1]).toBe(pts[99]);
    }
  });
});

describe("trade paging", () => {
  it("covers every trade exactly once", () => {
    for (const n of [0, 1, 49, 50, 51, 100, 101, 50000]) {
      const trades = Array.from({ length: n }, (_, index) => index);
      const seen: number[] = [];
      for (let page = 0; page * TRADE_PAGE_SIZE < Math.max(n, 1); page += 1) {
        seen.push(...(tradePage(trades, page) as number[]));
      }
      expect(seen).toEqual(trades);
    }
    expect(tradePage([1, 2, 3], -5)).toEqual([1, 2, 3]);
  });
});

describe("curve geometry and constants", () => {
  const point = (equity: string, openTime = 1): EquityPoint => ({
    open_time: openTime,
    equity,
    cash: "0",
  });

  it("puts higher equity higher on the screen and centres a flat series", () => {
    const coords = curveCoordinates([point("100", 1), point("200", 2), point("150", 3)])!
      .split(" ")
      .map((item) => item.split(",").map(Number) as [number, number]);
    expect(coords[0]![0]).toBe(1);
    expect(coords[2]![0]).toBe(99);
    expect(coords[1]![0]).toBe(50);
    expect(coords[1]![1]).toBeLessThan(coords[2]![1]);
    expect(coords[2]![1]).toBeLessThan(coords[0]![1]);
    expect(coords[0]![1]).toBe(39);
    expect(coords[1]![1]).toBe(1);
    expect(curveCoordinates([point("5"), point("5")])).toBe("1,20 99,20");
    expect(curveCoordinates([point("5")])).toBe("50,20");
    expect(curveCoordinates([point("NaN"), point("5")])).toBeNull();
    expect(curveCoordinates([point("Infinity"), point("5")])).toBeNull();
    expect(curveCoordinates([])).toBeNull();
  });

  it("trade columns, metric fields, and downloads are the full documented sets", () => {
    expect(TRADE_COLUMNS.length).toBe(21);
    expect(TRADE_COLUMNS).toContain("overlaps_suspect_outage");
    expect(TRADE_COLUMNS[0]).toBe("trade_id");
    expect(METRIC_FIELDS.length).toBe(8);
    expect(METRIC_FACTS.length).toBe(20);
    expect(DOWNLOADS.map((item) => item.kind)).toEqual([
      "trades.csv",
      "trades.json",
      "metrics.csv",
      "metrics.json",
      "result.json",
    ]);
    expect(DOWNLOADS.filter((item) => item.kind.endsWith(".csv")).every((item) => item.accept === "text/csv")).toBe(
      true,
    );
  });
});

function mark(equity: unknown, cash: unknown = "0", openTime: unknown = 1): unknown {
  return { open_time: openTime, equity, cash };
}

describe("equity point marks", () => {
  it("accepts finite numbers and the decimal text the backend stores", () => {
    for (const equity of ["0", "0.0", "0.10", "-0", "-0.0", "-1.5", "5", "100.00", "11480.52"]) {
      expect(isEquityPoint(mark(equity, "1")), equity).toBe(true);
    }
    for (const equity of [0, -0, 5, -1.5, 0.1, 11480.52]) {
      expect(isEquityPoint(mark(equity, 0)), String(equity)).toBe(true);
    }
    expect(isEquityPoint(mark("100.00", "5000.00", 1_700_000_000))).toBe(true);
  });

  it("rejects blank, hex, non-finite, and exponent text the backend cannot emit", () => {
    for (const equity of [
      "",
      " ",
      "\n",
      "\t",
      "0x10",
      "NaN",
      "Infinity",
      "-Infinity",
      "1e10",
      "1E+2",
      "1e-3",
      "+5",
      "00.1",
      "1.",
      ".5",
      " 1",
      "1 ",
    ]) {
      expect(isEquityPoint(mark(equity)), JSON.stringify(equity)).toBe(false);
    }
    for (const equity of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      expect(isEquityPoint(mark(equity)), String(equity)).toBe(false);
    }
    expect(isEquityPoint(mark("100.00", ""))).toBe(false);
    expect(isEquityPoint(mark("100.00", " "))).toBe(false);
    expect(isEquityPoint(mark("100.00", "0x10"))).toBe(false);
    expect(isEquityPoint(mark("100.00", "NaN"))).toBe(false);
    expect(isEquityPoint(mark("100.00", "Infinity"))).toBe(false);
    expect(isEquityPoint(mark("100.00", Number.NaN))).toBe(false);
    expect(isEquityPoint(mark("100.00", Number.POSITIVE_INFINITY))).toBe(false);
    expect(isEquityPoint(mark("5", "1", Number.NaN))).toBe(false);
    expect(isEquityPoint(mark("5", "1", Number.POSITIVE_INFINITY))).toBe(false);
    expect(isEquityPoint(mark("5", "1", "1"))).toBe(false);
    expect(isEquityPoint(null)).toBe(false);
    expect(isEquityPoint({})).toBe(false);
    expect(curveCoordinates([mark("0x10", "1") as EquityPoint, mark("5", "1") as EquityPoint])).toBeNull();
    expect(curveCoordinates([mark(" ", "1") as EquityPoint])).toBeNull();
  });
});
