import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AnalysisCharts } from "@/components/analysis-charts";
import { JobCharts } from "@/components/job-charts";
import { PriceChart } from "@/components/price-chart";
import { parseAnalysis, parseBars, parseIndicators } from "@/lib/charts";

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

type Call = { kind: string; data: Array<{ time: number; value?: number; close?: number }> };

function lib(): Call[] {
  const calls: Call[] = [];
  createChart.mockImplementation(() => ({
    addSeries: (definition: { kind: string }) => ({
      setData: (data: Call["data"]) => {
        calls.push({ kind: definition.kind, data });
      },
    }),
    remove: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
  }));
  return calls;
}

const times = [1_700_000_000, 1_700_000_060, 1_700_000_120, 1_700_000_180];

function candle(time: number, value: string) {
  return {
    open_time: time,
    open: value,
    high: value,
    low: value,
    close: value,
    volume: "1",
    synthetic: false,
  };
}

function barsDoc() {
  return {
    schema: "tradoortest.backtest_bars.v1",
    available: true,
    bar_size: "1m",
    slots_total: 4,
    slots_shown: 4,
    assets: [
      {
        asset_id: "sol",
        total: 4,
        shown: 4,
        rejected: 0,
        bars: times.map((time, index) => candle(time, String(10 + index))),
      },
    ],
  };
}

function series(id: string, indicator: string, values: string[]) {
  return {
    id,
    asset_id: "sol",
    indicator,
    output: null,
    total: 4,
    shown: 4,
    valid: 4,
    rejected: 0,
    points: times.map((time, index) => ({ open_time: time, value: values[index] })),
  };
}

function indicatorsDoc() {
  return {
    schema: "tradoortest.backtest_indicators.v1",
    available: true,
    slots_total: 4,
    slots_shown: 4,
    series: [
      series("sma_2", "sma", ["11.5", "12.5", "13.5", "14.5"]),
      series("rsi_2", "rsi", ["41.0", "42.0", "43.0", "44.0"]),
    ],
  };
}

const trades = [{ entry_time: String(times[0]), exit_time: String(times[3]) }];

function mount() {
  const calls = lib();
  const bars = parseBars(barsDoc());
  const indicators = parseIndicators(indicatorsDoc());
  if (!bars || !bars.available || !indicators) {
    throw new Error("fixture");
  }
  const view = render(<PriceChart bars={bars} indicators={indicators} trades={trades} />);
  return { calls, view };
}

describe("CR17 replay has no lookahead", () => {
  it("shows only the first candle, overlay, pane, caption, and marker", async () => {
    const { calls, view } = mount();
    await waitFor(() => expect(calls.some((call) => call.kind === "candle")).toBe(true));
    const candleCall = calls.filter((call) => call.kind === "candle").at(-1);
    expect(candleCall?.data.map((point) => point.time)).toEqual([times[0]]);
    for (const line of calls.filter((call) => call.kind === "line")) {
      expect(line.data.every((point) => point.time <= times[0]!)).toBe(true);
    }
    const text = view.container.textContent ?? "";
    expect(text).not.toContain("14.5");
    expect(text).not.toContain("44.0");
    expect(text).not.toContain("12.5");
    const placed = markers.mock.calls.at(-1)?.[1] as Array<{ time: number }>;
    expect(placed.map((marker) => marker.time)).toEqual([times[0]]);
    expect(view.container.querySelectorAll(".marker-list li")).toHaveLength(1);
  });

  it("steps one bar at a time and disables the end controls", async () => {
    const { calls } = mount();
    const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;
    expect(button("First bar").disabled).toBe(true);
    expect(button("Step back").disabled).toBe(true);
    expect(button("Step forward").disabled).toBe(false);
    fireEvent.click(button("Step forward"));
    await waitFor(() => expect(calls.filter((call) => call.kind === "candle").at(-1)?.data.length).toBe(2));
    expect(screen.getByText(/sma_2 · 11\.5 to 12\.5/)).toBeTruthy();
    fireEvent.click(button("Last bar"));
    await waitFor(() => expect(calls.filter((call) => call.kind === "candle").at(-1)?.data.length).toBe(4));
    expect(button("Step forward").disabled).toBe(true);
    expect(button("Last bar").disabled).toBe(true);
    expect(screen.getByText(/rsi_2 · 41\.0 to 44\.0/)).toBeTruthy();
  });
});

describe("CR17 failure states stay visible", () => {
  it("shows a draw failure and redraws after Retry chart", async () => {
    let fail = true;
    const calls: Call[] = [];
    createChart.mockImplementation(() => {
      if (fail) {
        throw new Error("boom");
      }
      return {
        addSeries: (definition: { kind: string }) => ({
          setData: (data: Call["data"]) => {
            calls.push({ kind: definition.kind, data });
          },
        }),
        remove: vi.fn(),
        timeScale: () => ({ fitContent: vi.fn() }),
      };
    });
    const bars = parseBars(barsDoc());
    if (!bars || !bars.available) {
      throw new Error("fixture");
    }
    render(<PriceChart bars={bars} indicators={null} trades={[]} />);
    await waitFor(() => expect(screen.getByText(/the price chart could not be drawn/)).toBeTruthy());
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry chart" }));
    await waitFor(() => expect(calls.some((call) => call.kind === "candle")).toBe(true));
    expect(screen.queryByText(/the price chart could not be drawn/)).toBeNull();
  });

  it("shows a failed fetch and recovers with Retry charts", async () => {
    lib();
    let attempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/bars") && attempts++ === 0) {
          throw new TypeError("net down");
        }
        const body = url.endsWith("/bars")
          ? barsDoc()
          : url.endsWith("/indicators")
            ? indicatorsDoc()
            : {
                schema: "tradoortest.backtest_analysis.v1",
                available: false,
                timezone: "UTC",
                trades_total: 0,
                trades_used: 0,
              };
        return new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    render(<JobCharts jobId={"a".repeat(32)} trades={[]} />);
    await waitFor(() => expect(screen.getByText(/^bars: /)).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Retry charts" }));
    await waitFor(() => expect(screen.getByText(/Bar 1 of 4/)).toBeTruthy());
    expect(screen.queryByText(/^bars: /)).toBeNull();
  });
});

describe("CR17 analysis with an empty hour list says unavailable", () => {
  it("does not draw an empty hour chart", () => {
    const analysis = parseAnalysis({
      schema: "tradoortest.backtest_analysis.v1",
      available: true,
      trades_total: 1,
      trades_used: 1,
      hours: [],
      weekdays: [{ weekday_utc: 0, weekday: "Monday", count: 1 }],
    });
    render(<AnalysisCharts analysis={analysis!} />);
    expect(screen.getByText("Time of day unavailable")).toBeTruthy();
    expect(document.querySelector('[aria-label="UTC hour of entry"]')).toBeNull();
  });
});
