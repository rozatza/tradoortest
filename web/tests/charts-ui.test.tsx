import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JobCharts } from "@/components/job-charts";

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
    expect(screen.getByText(/2 of 4 points valid/)).toBeTruthy();
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
});
