import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobCharts } from "@/components/job-charts";
import { PriceChart } from "@/components/price-chart";
import { parseBars, parseIndicators } from "@/lib/charts";

const markers = vi.hoisted(() => vi.fn());
const createChart = vi.hoisted(() => vi.fn());

vi.mock("lightweight-charts", () => ({
  createChart,
  CandlestickSeries: { kind: "candle" },
  LineSeries: { kind: "line" },
  createSeriesMarkers: markers,
}));

afterEach(() => {
  vi.unstubAllGlobals();
  markers.mockReset();
  createChart.mockReset();
});

function chartLib() {
  const setData = vi.fn();
  createChart.mockImplementation(() => ({
    addSeries: () => ({ setData }),
    remove: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
  }));
  return setData;
}

function bars() {
  return {
    schema: "tradoortest.backtest_bars.v1",
    available: true,
    bar_size: "1m",
    slots_total: 4,
    slots_shown: 2,
    assets: [
      {
        asset_id: "sol",
        total: 4,
        shown: 2,
        rejected: 1,
        bars: [
          candle(1_700_000_000, "10"),
          candle(1_700_000_060, "20"),
          { ...candle(1_700_000_120, "nope"), close: "nope" },
        ],
      },
    ],
  };
}

function candle(openTime: number, close: string) {
  return {
    open_time: openTime,
    open: close,
    high: close,
    low: close,
    close,
    volume: "1",
    synthetic: false,
  };
}

function indicators() {
  return {
    schema: "tradoortest.backtest_indicators.v1",
    available: true,
    slots_total: 4,
    slots_shown: 2,
    series: [
      {
        id: "sma_2",
        asset_id: "sol",
        indicator: "sma",
        output: null,
        total: 4,
        shown: 2,
        valid: 1,
        rejected: 1,
        points: [
          { open_time: 1_700_000_000, value: null },
          { open_time: 1_700_000_060, value: "15.0" },
          { open_time: 1_700_000_120, value: "bad" },
        ],
      },
    ],
  };
}

function analysis(body: unknown) {
  return {
    schema: "tradoortest.backtest_analysis.v1",
    available: true,
    timezone: "UTC",
    trades_total: 3,
    trades_used: 2,
    hours: [
      { hour_utc: 0, count: 2 },
      { hour_utc: 14, count: 1 },
    ],
    weekdays: [{ weekday_utc: 3, weekday: "Thursday", count: 2 }],
    ...((body as object) ?? {}),
  };
}

function install(nextAnalysis: unknown = analysis(null)) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.endsWith("/bars")
        ? bars()
        : url.endsWith("/indicators")
          ? indicators()
          : url.endsWith("/analysis")
            ? nextAnalysis
            : { error: { code: "not_found", message: "missing", path: null } };
      const status = url.endsWith("/bars") || url.endsWith("/indicators") || url.endsWith("/analysis") ? 200 : 404;
      return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
}

describe("price chart", () => {
  it("steps one bar at a time and aligns markers to the trade log", async () => {
    chartLib();
    install();
    render(
      <JobCharts
        jobId={"ab".repeat(16)}
        trades={[
          { entry_time: "1700000000", exit_time: "1700000060" },
          { entry_time: "1700000001", exit_time: null },
        ]}
      />,
    );
    expect(await screen.findByText(/Bar 1 of 2/)).toBeTruthy();
    expect(screen.getByText(/2 of 4 bars shown/)).toBeTruthy();
    expect(screen.queryByText(/2 of 4 points valid/)).toBeNull();
    expect(document.querySelector("[data-marker-time='1700000000']")?.getAttribute("data-marker-kind")).toBe(
      "entry",
    );
    expect(document.querySelector("[data-marker-time='1700000060']")).toBeNull();
    expect(screen.queryByText("entry 1700000001")).toBeNull();
    await waitFor(() => expect(markers).toHaveBeenCalled());
    const firstMarkers = markers.mock.calls[0]?.[1] as { time: number; text: string }[];
    expect(firstMarkers.map((marker) => marker.time)).toEqual([1_700_000_000]);
    const options = createChart.mock.calls[0]?.[1] as { layout?: { attributionLogo?: boolean } };
    expect(options.layout?.attributionLogo).not.toBe(false);
    expect(JSON.stringify(options)).not.toContain("attributionLogo");

    fireEvent.click(screen.getByRole("button", { name: "Step forward" }));
    expect(screen.getByText(/Bar 2 of 2/)).toBeTruthy();
    expect(screen.getByText(/close 20/)).toBeTruthy();
    expect(screen.getByText(/2 of 4 points valid/)).toBeTruthy();
    const exit = document.querySelector("[data-marker-time='1700000060']");
    expect(exit?.getAttribute("data-marker-kind")).toBe("exit");
    expect(exit?.textContent).toBe("exit 1700000060");
    expect(screen.getByText("sma_2 · 15.0 to 15.0 · 1 of 3 points valid · 2 of 4 points shown")).toBeTruthy();
    expect(screen.getByText(/2 of 3 markers shown/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "https://www.tradingview.com/" }).getAttribute("href")).toBe(
      "https://www.tradingview.com/",
    );
    expect(screen.getByText(/TradingView Lightweight Charts/)).toBeTruthy();
  });

  it("shows the API hour and weekday counts and nothing for an empty log", async () => {
    chartLib();
    install();
    const view = render(<JobCharts jobId={"ab".repeat(16)} trades={[]} />);
    expect(await screen.findByText("00 UTC")).toBeTruthy();
    expect(screen.getByText("14 UTC")).toBeTruthy();
    expect(screen.getByText("Thursday")).toBeTruthy();
    expect(screen.getAllByText("2").length).toBeGreaterThan(0);
    expect(screen.queryByText("01 UTC")).toBeNull();
    expect(screen.queryByText("Monday")).toBeNull();
    expect(screen.getByText("2 of 3 trades used")).toBeTruthy();
    view.unmount();

    install({
      schema: "tradoortest.backtest_analysis.v1",
      available: false,
      timezone: "UTC",
      trades_total: 0,
      trades_used: 0,
    });
    render(<JobCharts jobId={"cd".repeat(16)} trades={[]} />);
    expect(await screen.findByText("Time of day unavailable")).toBeTruthy();
    expect(screen.getByText("Weekday unavailable")).toBeTruthy();
    expect(screen.queryByText("00 UTC")).toBeNull();
  });

  it("shows the valid-point note and no fabricated close or SMA from a loose number parse", async () => {
    const payloads: unknown[] = [];
    const setData = vi.fn((data: unknown) => {
      payloads.push(data);
    });
    createChart.mockImplementation(() => ({
      addSeries: () => ({ setData }),
      remove: vi.fn(),
      timeScale: () => ({ fitContent: vi.fn() }),
    }));
    const junk = ["0x10", "1e10", " ", "", "NaN", "Infinity"];
    const start = 1_700_000_000;
    const parsedBars = parseBars({
      schema: "tradoortest.backtest_bars.v1",
      available: true,
      bar_size: "1m",
      slots_total: 7,
      slots_shown: 7,
      assets: [
        {
          asset_id: "sol",
          total: 7,
          shown: 7,
          rejected: 0,
          bars: [
            candle(start, "10"),
            ...junk.map((close, index) => ({
              ...candle(start + (index + 1) * 60, "10"),
              close,
            })),
          ],
        },
      ],
    });
    const parsedIndicators = parseIndicators({
      schema: "tradoortest.backtest_indicators.v1",
      available: true,
      slots_total: 7,
      slots_shown: 7,
      series: [
        {
          id: "sma_2",
          asset_id: "sol",
          indicator: "sma",
          output: null,
          total: 7,
          shown: 7,
          valid: 1,
          rejected: 0,
          points: [
            { open_time: start, value: "15.0" },
            ...junk.map((value, index) => ({
              open_time: start + (index + 1) * 60,
              value,
            })),
          ],
        },
      ],
    });
    expect(parsedBars?.available).toBe(true);
    expect(parsedIndicators?.available).toBe(true);
    if (!parsedBars?.available || !parsedIndicators?.available) {
      return;
    }
    render(<PriceChart bars={parsedBars} indicators={parsedIndicators} trades={[]} />);
    expect(await screen.findByText(/Bar 1 of 1/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Last bar" }));
    expect(screen.getByText(/Bar 1 of 1/)).toBeTruthy();
    expect(screen.getAllByText(/1 of 7 points valid/).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/open 10 high 10 low 10 close 10 volume 1/)).toBeTruthy();
    expect(screen.getByText("sma_2 · 15.0 to 15.0 · 1 of 7 points valid")).toBeTruthy();
    expect(screen.queryByText(/0x10/)).toBeNull();
    expect(screen.queryByText(/1e10/)).toBeNull();
    expect(screen.queryByText(/NaN/)).toBeNull();
    expect(screen.queryByText(/Infinity/)).toBeNull();
    expect(screen.queryByText(/close 16/)).toBeNull();
    expect(screen.queryByText(/close 0\b/)).toBeNull();
    await waitFor(() => expect(setData.mock.calls.length).toBeGreaterThanOrEqual(2));
    const drawn = JSON.stringify(payloads);
    expect(drawn).not.toContain("0x10");
    expect(drawn).not.toContain("1e10");
    expect(drawn).not.toContain("10000000000");
    const closes = payloads.flatMap((payload) =>
      Array.isArray(payload)
        ? payload.flatMap((row) => {
            const close = (row as { close?: unknown }).close;
            return typeof close === "number" ? [close] : [];
          })
        : [],
    );
    const values = payloads.flatMap((payload) =>
      Array.isArray(payload)
        ? payload.flatMap((row) => {
            const value = (row as { value?: unknown }).value;
            return typeof value === "number" ? [value] : [];
          })
        : [],
    );
    expect(closes).toEqual([10]);
    expect(values).toEqual([15]);
  });
});

describe("tiny prices stay on the chart", () => {
  it("shows the drop count when every bar is an exponent", () => {
    const parsed = parseBars({
      schema: "tradoortest.backtest_bars.v1",
      available: true,
      bar_size: "1m",
      slots_total: 2,
      slots_shown: 2,
      assets: [
        {
          asset_id: "bonk",
          total: 2,
          shown: 2,
          rejected: 0,
          bars: [candle(1_700_000_000, "2.2e-05"), candle(1_700_000_060, "9.99e-05")],
        },
      ],
    });
    if (!parsed || !parsed.available) {
      throw new Error("parse");
    }
    render(<PriceChart bars={parsed} indicators={null} trades={[]} />);
    expect(screen.getByText(/Price chart unavailable/)).toBeTruthy();
    expect(screen.getByText(/0 of 2 points valid/)).toBeTruthy();
  });

  it("draws a plain 0.000022 close and a series that crosses 0.0001", async () => {
    const formats: Array<{ precision?: number; minMove?: number; type?: string }> = [];
    createChart.mockImplementation(() => ({
      addSeries: (_definition: unknown, options: { priceFormat?: { precision?: number; minMove?: number; type?: string } }) => {
        if (options.priceFormat) {
          formats.push(options.priceFormat);
        }
        return { setData: vi.fn() };
      },
      remove: vi.fn(),
      timeScale: () => ({ fitContent: vi.fn() }),
    }));
    const below = "0.0000999";
    const above = "0.00022";
    const parsed = parseBars({
      schema: "tradoortest.backtest_bars.v1",
      available: true,
      bar_size: "1m",
      slots_total: 2,
      slots_shown: 2,
      assets: [
        {
          asset_id: "bonk",
          total: 2,
          shown: 2,
          rejected: 0,
          bars: [candle(1_700_000_000, "0.000022"), candle(1_700_000_060, below), candle(1_700_000_120, above)],
        },
      ],
    });
    if (!parsed || !parsed.available) {
      throw new Error("parse");
    }
    expect(parsed.assets[0]?.dropped).toBe(0);
    expect(parsed.assets[0]?.bars.map((bar) => bar.close)).toEqual(["0.000022", below, above]);
    render(<PriceChart bars={parsed} indicators={null} trades={[]} />);
    expect(screen.getByText(/close 0\.000022/)).toBeTruthy();
    expect(screen.queryByText(/Price chart unavailable/)).toBeNull();
    expect(screen.queryByText(/points valid/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Last bar" }));
    expect(screen.getByText(/close 0\.00022/)).toBeTruthy();
    await waitFor(() => expect(formats.length).toBeGreaterThan(0));
    expect(formats[0]?.type).toBe("price");
    expect(formats[0]?.precision).toBeGreaterThanOrEqual(6);
    expect(formats[0]?.minMove).toBeLessThan(0.01);
  });
});
