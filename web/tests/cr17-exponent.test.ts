// Adapted from the CR kill-test. The old backend spelled 2.2e-05 as an exponent
// and this file expected the client to accept that text. The fix keeps the client
// grammar strict and spells the same magnitudes as plain decimals, so these cases
// accept that spelling and still reject the exponent form.
import { describe, expect, it } from "vitest";

import { parseBars, parseIndicators } from "@/lib/charts";

const tiny = "0.0000215217948223";
const sma = "0.000021937";

describe("CR17 backend spelling of small prices is accepted", () => {
  it("accepts the plain decimal a $0.00002 bar is stored as", () => {
    const parsed = parseBars({
      schema: "tradoortest.backtest_bars.v1",
      available: true,
      bar_size: "1h",
      slots_total: 1,
      slots_shown: 1,
      assets: [
        {
          asset_id: "bonk",
          total: 1,
          shown: 1,
          rejected: 0,
          bars: [
            {
              open_time: 1767225600,
              open: tiny,
              high: tiny,
              low: tiny,
              close: tiny,
              volume: "5",
              synthetic: false,
            },
          ],
        },
      ],
    });
    if (!parsed || !parsed.available) {
      throw new Error("parse");
    }
    expect(parsed.assets[0]?.bars).toHaveLength(1);
    expect(parsed.assets[0]?.bars[0]?.close).toBe(tiny);
    expect(parsed.assets[0]?.dropped).toBe(0);
  });

  it("accepts a plain-decimal indicator and still rejects an exponent", () => {
    const parsed = parseIndicators({
      schema: "tradoortest.backtest_indicators.v1",
      available: true,
      slots_total: 2,
      slots_shown: 2,
      series: [
        {
          id: "sma_5",
          asset_id: "bonk",
          indicator: "sma",
          output: null,
          total: 2,
          shown: 2,
          valid: 1,
          rejected: 1,
          points: [
            { open_time: 1767225600, value: sma },
            { open_time: 1767229200, value: "2.1937e-05" },
          ],
        },
      ],
    });
    if (!parsed || !parsed.available) {
      throw new Error("parse");
    }
    expect(parsed.series[0]?.dropped).toBe(1);
    expect(parsed.series[0]?.points[0]?.valueText).toBe(sma);
    expect(parsed.series[0]?.points.some((point) => point.valueText === "2.1937e-05")).toBe(false);
  });
});
