import { readFileSync } from "node:fs";
import path from "node:path";

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BacktestResults } from "@/components/backtest-results";
import {
  BACKTEST_TIMEOUT_MS,
  EQUITY_DRAW_CAP,
  TRADE_PAGE_SIZE,
  isJobId,
  pointsForCurve,
  type EquityPoint,
} from "@/lib/backtest";
import { isAgentId } from "@/lib/upstream";

const JOB_ID = "ab".repeat(16);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("backtest results", () => {
  it("uses the same id check as the proxy and keeps storage and rounding out of the view", () => {
    const samples = ["", "A".repeat(32), "g".repeat(32), "a".repeat(31), JOB_ID, `${JOB_ID}\n`];
    for (const sample of samples) {
      expect(isJobId(sample)).toBe(isAgentId(sample));
    }
    const root = path.resolve(import.meta.dirname, "..");
    for (const file of ["lib/backtest.ts", "components/backtest-results.tsx"]) {
      const text = readFileSync(path.join(root, file), "utf8");
      for (const token of ["localStorage", "sessionStorage", "indexedDB", "caches", "toFixed", "privateKey", "signTransaction"]) {
        expect(text, `${file} ${token}`).not.toContain(token);
      }
    }
  });

  it("shows an invalid id without calling the API", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<BacktestResults />);
    fireEvent.change(screen.getByLabelText("Job id"), { target: { value: "not-a-job" } });
    fireEvent.click(screen.getByRole("button", { name: "Load results" }));
    expectText(await screen.findByRole("alert"), "invalid_id: job_id is invalid");
    expectEnabled(screen.getByRole("button", { name: "Load results" }));
    expectEnabled(screen.getByLabelText("Job id"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows a 404 and still accepts another id", async () => {
    const nextId = "c".repeat(32);
    const fetchMock = install((url) => {
      if (url.includes(nextId)) {
        return jsonResponse({ ...jobBody("queued"), job_id: nextId });
      }
      return jsonResponse({ error: { code: "not_found", message: "backtest job was not found", path: null } }, 404);
    });
    render(<BacktestResults />);
    submit(JOB_ID);
    expectText(await screen.findByRole("alert"), "not_found: backtest job was not found");
    expectEnabled(screen.getByRole("button", { name: "Load results" }));
    fireEvent.change(screen.getByLabelText("Job id"), { target: { value: nextId } });
    fireEvent.click(screen.getByRole("button", { name: "Load results" }));
    expectText(await screen.findByRole("status"), "Backtest is queued. Results are not ready.");
    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("shows expiry, a JSON 500, a non-JSON body, and a rejected fetch", async () => {
    const cases = [
      {
        response: jsonResponse(
          { error: { code: "expired", message: "the backtest result has expired", path: null } },
          410,
        ),
        text: "expired: the backtest result has expired",
      },
      {
        response: jsonResponse({ error: { code: "internal_error", message: "the author API failed", path: null } }, 500),
        text: "internal_error: the author API failed",
      },
      {
        response: new Response("nope", { status: 500, headers: { "content-type": "text/plain" } }),
        text: "invalid_response: the response was not JSON",
      },
      {
        response: new Response("not-json", { status: 200, headers: { "content-type": "text/plain" } }),
        text: "invalid_response: the response was not JSON",
      },
    ];
    for (const item of cases) {
      install(() => item.response);
      const view = render(<BacktestResults />);
      submit(JOB_ID);
      expectText(await screen.findByRole("alert"), item.text);
      expectEnabled(screen.getByRole("button", { name: "Load results" }));
      expectEnabled(screen.getByLabelText("Job id"));
      view.unmount();
    }
    install(() => Promise.reject(new TypeError("failed to fetch")));
    render(<BacktestResults />);
    submit(JOB_ID);
    expectText(await screen.findByRole("alert"), "network_error: the request could not be completed");
    expectEnabled(screen.getByRole("button", { name: "Load results" }));
  });

  it("shows a timeout and leaves the load control enabled", async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        return new Promise((_resolve, reject) => {
          const signal = init?.signal;
          if (!signal) {
            reject(new Error("missing signal"));
            return;
          }
          signal.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      }),
    );
    render(<BacktestResults />);
    submit(JOB_ID);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(BACKTEST_TIMEOUT_MS);
    });
    expectText(screen.getByRole("alert"), "timeout: the request timed out");
    expectEnabled(screen.getByRole("button", { name: "Load results" }));
    expectEnabled(screen.getByLabelText("Job id"));
  });

  it("shows a failed job and a job that is not ready", async () => {
    install(() =>
      jsonResponse({
        job_id: JOB_ID,
        status: "failed",
        error: { code: "indicator_error", message: "sma failed", path: null },
      }),
    );
    const failed = render(<BacktestResults />);
    submit(JOB_ID);
    expectText(await screen.findByRole("alert"), "indicator_error: sma failed");
    expect(screen.queryByRole("img")).toBeNull();
    failed.unmount();

    const fetchMock = install((url) => {
      expect(url).not.toContain("/results");
      return jsonResponse(jobBody("queued"));
    });
    render(<BacktestResults />);
    submit(JOB_ID);
    expectText(await screen.findByRole("status"), "Backtest is queued. Results are not ready.");
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByText("0.1000000000")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expectEnabled(screen.getByRole("button", { name: "Load results" }));
  });

  it("renders the API strings and does not derive a return or a curve", async () => {
    install((url) => {
      if (url.endsWith("/results")) {
        return jsonResponse(resultBody());
      }
      return jsonResponse(jobBody("succeeded"));
    });
    render(<BacktestResults />);
    submit(JOB_ID);
    expect(await screen.findByRole("img", { name: "Equity curve from 10000.00 to 11480.52" })).toBeTruthy();
    const net = field("net_return");
    expectText(net.querySelector("dd"), "0.1000000000");
    expect(net.querySelector("dd")?.getAttribute("title")).toBe("0.1000000000");
    expect(screen.queryByText("0.148052")).toBeNull();
    const sharpe = field("sharpe");
    expectText(sharpe, "unavailable");
    expectText(sharpe, "too_few_returns");
    expect(sharpe.querySelector("dd")?.hasAttribute("title")).toBe(false);
    expectText(field("bar_count"), "9");
    expectText(field("suspect_outage"), "false");
    expect(screen.getByText("sma on sol is computed across 2 suspect_outage bars")).toBeTruthy();
    const exit = screen.getAllByRole("cell").find((cell) => cell.textContent === "unavailable");
    expect(exit).toBeTruthy();
    expect(exit?.hasAttribute("title")).toBe(false);
    expect(screen.getByRole("cell", { name: "1480.52" }).getAttribute("title")).toBe("1480.52");
  });

  it("says the curve is unavailable when the result has fills but no curve", async () => {
    const body = resultBody();
    delete body.equity_curve;
    body.fills = [{ equity_after: "1.11", price: "9.99" }];
    install((url) => (url.endsWith("/results") ? jsonResponse(body) : jsonResponse(jobBody("succeeded"))));
    render(<BacktestResults />);
    submit(JOB_ID);
    expect(await screen.findByText("Equity curve unavailable")).toBeTruthy();
    expect(screen.queryByText("1.11")).toBeNull();
    expect(screen.queryByText("9.99")).toBeNull();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("downloads the export bytes unchanged and keeps the buttons usable after a failure", async () => {
    const exact = '{"schema" : "tradoortest.metrics.v1","net_return":"9.9999999999"}\n';
    let results = 0;
    install((url) => {
      if (url.endsWith("/exports/metrics.json")) {
        return new Response(exact, { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/exports/trades.csv")) {
        return jsonResponse({ error: { code: "expired", message: "the backtest result has expired", path: null } }, 410);
      }
      if (url.endsWith("/results")) {
        results += 1;
        if (results > 1) {
          return new Response('{"final_equity" : "11480.52"}', {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return jsonResponse(resultBody());
      }
      return jsonResponse(jobBody("succeeded"));
    });
    const blobs: Blob[] = [];
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      blobs.push(blob as Blob);
      return "blob:backtest";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    render(<BacktestResults />);
    submit(JOB_ID);
    await screen.findByRole("img");
    fireEvent.click(screen.getByRole("button", { name: "Download metrics.json" }));
    await waitFor(() => expect(blobs).toHaveLength(1));
    expect(await blobs[0]?.text()).toBe(exact);
    fireEvent.click(screen.getByRole("button", { name: "Download result JSON" }));
    await waitFor(() => expect(blobs).toHaveLength(2));
    expect(await blobs[1]?.text()).toBe('{"final_equity" : "11480.52"}');
    fireEvent.click(screen.getByRole("button", { name: "Download trades.csv" }));
    expectText(await screen.findByRole("alert"), "expired: the backtest result has expired");
    expectEnabled(screen.getByRole("button", { name: "Download trades.csv" }));
    expectEnabled(screen.getByRole("button", { name: "Load results" }));
    expect(screen.getByRole("img")).toBeTruthy();
  });

  it("pages 50000 trades and caps a 200000-point curve without shrinking the export", async () => {
    const curve = largeCurve();
    const drawn = pointsForCurve(curve, EQUITY_DRAW_CAP);
    expect(EQUITY_DRAW_CAP).toBeLessThanOrEqual(500);
    expect(TRADE_PAGE_SIZE).toBeLessThanOrEqual(50);
    expect(drawn.length).toBeGreaterThan(2);
    expect(drawn.length).toBeLessThanOrEqual(500);
    expect(drawn[0]).toBe(curve[0]);
    expect(drawn[drawn.length - 1]).toBe(curve[curve.length - 1]);
    const identities = new Set(curve);
    expect(drawn.every((point) => identities.has(point))).toBe(true);
    const trades = largeTrades();
    const body = resultBody();
    body.equity_curve = curve;
    body.trades = trades;
    body.metrics = {
      ...(body.metrics as Record<string, unknown>),
      bar_count: 3,
    };
    install((url) => {
      if (url.includes("/exports/")) {
        expect(url).not.toContain("offset");
        expect(url).not.toContain("limit");
        return new Response("FULL-EXPORT", { status: 200, headers: { "content-type": "text/csv" } });
      }
      if (url.endsWith("/results")) {
        return jsonResponse(body);
      }
      return jsonResponse(jobBody("succeeded"));
    });
    const started = performance.now();
    render(<BacktestResults />);
    submit(JOB_ID);
    const chart = await screen.findByRole("img", { name: /11480.52/ });
    expect(performance.now() - started).toBeLessThan(8_000);
    const painted = chart.querySelector("polyline")?.getAttribute("points")?.split(" ").length ?? 0;
    expect(painted).toBeLessThanOrEqual(500);
    expect(painted).toBe(Number(chart.getAttribute("data-drawn")));
    expect(chart.getAttribute("data-total")).toBe("200000");
    expect(screen.getAllByRole("row").length).toBeLessThanOrEqual(51);
    expect(screen.getByText("1-50 of 50000")).toBeTruthy();
    expect(screen.getByRole("cell", { name: "t1" })).toBeTruthy();
    expect(screen.queryByRole("cell", { name: "t51" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("51-100 of 50000")).toBeTruthy();
    expect(screen.getByRole("cell", { name: "t51" })).toBeTruthy();
    const blobs: Blob[] = [];
    vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => {
      blobs.push(blob as Blob);
      return "blob:full";
    });
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    fireEvent.click(screen.getByRole("button", { name: "Download trades.csv" }));
    await waitFor(() => expect(blobs).toHaveLength(1));
    expect(await blobs[0]?.text()).toBe("FULL-EXPORT");
  });
});

function expectText(element: Element | null | undefined, value: string) {
  expect(element?.textContent ?? "").toContain(value);
}

function expectEnabled(element: HTMLElement) {
  expect((element as HTMLButtonElement).disabled).toBe(false);
}

function submit(jobId: string) {
  fireEvent.change(screen.getByLabelText("Job id"), { target: { value: jobId } });
  fireEvent.click(screen.getByRole("button", { name: "Load results" }));
}

function field(name: string): HTMLElement {
  const block = screen.getByText(name).closest("div");
  if (!block) {
    throw new Error(name);
  }
  return block;
}

function install(handler: (url: string) => Response | Promise<Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    return handler(url);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function jobBody(status: string) {
  return {
    job_id: JOB_ID,
    request_key: "curve",
    user_id: "local-user",
    status,
    error: null,
  };
}

function resultBody(): Record<string, unknown> {
  return {
    schema: "tradoortest.backtest_result.v1",
    final_equity: "11480.52",
    final_cash: "11480.52",
    lints: ["sma on sol is computed across 2 suspect_outage bars"],
    equity_curve: [
      { open_time: 1, equity: "10000.00", cash: "10000.00" },
      { open_time: 2, equity: "9992.50", cash: "5000.00" },
      { open_time: 3, equity: "11480.52", cash: "11480.52" },
    ],
    metrics: {
      values: {
        net_return: "0.1000000000",
        annualized_return: null,
        max_drawdown: "0.00075",
        sharpe: null,
        sortino: null,
        calmar: null,
        win_rate: "1.0000000000",
        payoff_ratio: null,
      },
      undefined: {
        annualized_return: "too_short",
        sharpe: "too_few_returns",
        sortino: "too_few_returns",
        calmar: "too_short",
        payoff_ratio: "no_losses",
      },
      initial_equity: "10000.00",
      final_equity: "11480.52",
      bar_count: 9,
      bars_per_year: 525600,
      grid_bars: 9,
      missing_bars: 0,
      synthetic_bars: 0,
      long_no_trade_bars: 0,
      long_no_trade_minutes: 0,
      long_no_trade_threshold: 120,
      suspect_outage: false,
      suspect_outage_bars: 0,
      suspect_outage_minutes: 0,
      missing_bars_in_position: 0,
      synthetic_bars_in_position: 0,
      long_no_trade_bars_in_position: 0,
      long_no_trade_minutes_in_position: 0,
      suspect_outage_bars_in_position: 0,
      suspect_outage_minutes_in_position: 0,
      take_profit_limit_below_low: 0,
    },
    trades: [
      {
        trade_id: "1",
        asset_id: "sol",
        status: "closed",
        entry_time: "1700000060",
        exit_time: null,
        qty: "49.925037481",
        entry_price: "100.05",
        exit_price: null,
        entry_notional: "4995.00",
        exit_notional: "0.00",
        commission: "5.00",
        realized_pnl: "1480.52",
        unrealized_pnl: "0.00",
        limit_below_low: "false",
        overlaps_suspect_outage: "false",
        synthetic_bars: "0",
        missing_bars: "0",
        long_no_trade_bars: "0",
        long_no_trade_minutes: "0",
        suspect_outage_bars: "0",
        suspect_outage_minutes: "0",
      },
    ],
    fills: [],
  };
}

function largeCurve(): EquityPoint[] {
  const points = new Array<EquityPoint>(200_000);
  for (let index = 0; index < points.length; index += 1) {
    points[index] = {
      open_time: 1_700_000_000 + index,
      equity: index === points.length - 1 ? "11480.52" : index % 2 === 0 ? "10000.00" : "10001.25",
      cash: "5000.00",
    };
  }
  return points;
}

function largeTrades(): Array<Record<string, string>> {
  const trades = new Array<Record<string, string>>(50_000);
  for (let index = 0; index < trades.length; index += 1) {
    trades[index] = { trade_id: `t${index + 1}` };
  }
  return trades;
}
