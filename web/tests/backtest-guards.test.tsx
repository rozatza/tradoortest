import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BacktestResults } from "@/components/backtest-results";
import { METRIC_FACTS, METRIC_FIELDS, TRADE_COLUMNS } from "@/lib/backtest";

const ID = "ab".repeat(16);
const ID2 = "cd".repeat(16);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function trade(index: number): Record<string, string> {
  const row: Record<string, string> = {};
  for (const column of TRADE_COLUMNS) {
    row[column] = `${column}-${index}`;
  }
  return row;
}

function result(over: Record<string, unknown> = {}, nTrades = 3): Record<string, unknown> {
  const values: Record<string, string | null> = {};
  for (const field of METRIC_FIELDS) {
    values[field] = "1.5";
  }
  const metrics: Record<string, unknown> = { values, undefined: {} };
  for (const field of METRIC_FACTS) {
    metrics[field] = "7";
  }
  return {
    schema: "tradoortest.backtest_result.v1",
    final_equity: "111.00",
    final_cash: "222.00",
    lints: [],
    metrics,
    trades: Array.from({ length: nTrades }, (_, index) => trade(index)),
    equity_curve: [
      { open_time: 1, equity: "100.00", cash: "1" },
      { open_time: 2, equity: "120.00", cash: "1" },
      { open_time: 3, equity: "90.00", cash: "1" },
    ],
    ...over,
  };
}

function install(body: unknown | ((id: string) => Response)): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const id = (/backtests\/([0-9a-f]{32})/.exec(url) ?? [])[1] ?? "";
    if (url.endsWith("/results") || url.includes("/exports/")) {
      return typeof body === "function" ? body(id) : jsonResponse(body);
    }
    return jsonResponse({ job_id: id, status: "succeeded" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function load(id = ID) {
  fireEvent.change(screen.getByLabelText("Job id"), { target: { value: id } });
  fireEvent.click(screen.getByRole("button", { name: "Load results" }));
}

function factValues(field: string): (string | null)[] {
  const label = screen.getAllByText(field).find((element) => element.tagName === "DT");
  expect(label).toBeTruthy();
  return [...label!.parentElement!.querySelectorAll("dd")].map((cell) => cell.textContent);
}

describe("missing fields stay unavailable", () => {
  it("missing metric, fact, headline, and trade cells say unavailable", async () => {
    const body = result({}, 2);
    delete body.final_equity;
    delete body.final_cash;
    delete body.schema;
    const metrics = body.metrics as { values: Record<string, unknown> } & Record<string, unknown>;
    for (const field of METRIC_FIELDS) {
      delete metrics.values[field];
    }
    for (const field of METRIC_FACTS) {
      delete metrics[field];
    }
    body.trades = [{}, { trade_id: null, asset_id: "" }];
    install(body);
    render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    for (const field of [...METRIC_FIELDS, ...METRIC_FACTS]) {
      expect(factValues(field), field).toEqual(["unavailable"]);
    }
    expect(document.querySelector(".meta")!.textContent).toBe(
      "Schema unavailableFinal equity unavailableFinal cash unavailable",
    );
    const cells = [...document.querySelectorAll(".trade-log tbody td")].map((cell) => cell.textContent);
    expect(cells.length).toBeGreaterThan(0);
    expect(new Set(cells)).toEqual(new Set(["unavailable"]));
  });

  it("headline fields map to their own API field", async () => {
    install(result());
    render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    expect(document.querySelector(".meta")!.textContent).toBe(
      "Schema tradoortest.backtest_result.v1Final equity 111.00Final cash 222.00",
    );
  });

  it("reads metric values from values, and a reason only when the value is null", async () => {
    const planted = result();
    planted.metrics = { sharpe: "9.9", bar_count: "5", undefined: { net_return: "r2" } };
    install(planted);
    const first = render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    expect(factValues("sharpe")).toEqual(["unavailable"]);
    expect(factValues("bar_count")).toEqual(["5"]);
    first.unmount();

    const second = result();
    const metrics = second.metrics as {
      values: Record<string, unknown>;
      undefined: Record<string, unknown>;
    };
    metrics.values.net_return = "0.1";
    metrics.values.sharpe = null;
    metrics.undefined = { net_return: "stale", sharpe: "too_few" };
    install(second);
    render(<BacktestResults />);
    await load(ID2);
    await screen.findByText("Metrics");
    expect(factValues("net_return")).toEqual(["0.1"]);
    expect(factValues("sharpe")).toEqual(["unavailable", "too_few"]);
  });

  it("ignores blank, hex, and null curve points and draws the remaining decimals", async () => {
    install(
      result({
        equity_curve: [
          { open_time: 1, equity: "", cash: "1" },
          { open_time: 2, equity: "0x10", cash: "1" },
          null,
          { open_time: 3, equity: "100.00", cash: "1" },
          { open_time: 4, equity: "110.00", cash: "1" },
        ],
      }),
    );
    render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    expect(document.querySelector(".equity figcaption")!.textContent).toBe("100.00 to 110.00");
    expect(document.querySelector("polyline")!.getAttribute("points")).toBe("1,39 99,1");
  });

  it("does not draw a fabricated 0 for a non-finite or blank mark", async () => {
    for (const bad of ["NaN", "Infinity", "", " ", "0x10", "1e10"]) {
      install(
        result({
          equity_curve: [
            { open_time: 1, equity: bad, cash: "1" },
            { open_time: 2, equity: "5", cash: "1" },
          ],
        }),
      );
      const view = render(<BacktestResults />);
      await load();
      await screen.findByText("Metrics");
      expect(document.querySelector("polyline"), JSON.stringify(bad)).toBeNull();
      view.unmount();
    }
  });

  it("says unavailable for a non-object trade row and a non-array lint list", async () => {
    install(result({ trades: [1, "x"], lints: "nope" }));
    render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    expect([...document.querySelectorAll(".trade-log tbody td")].map((cell) => cell.textContent)).toEqual([
      "unavailable",
      "unavailable",
    ]);
    expect(screen.getByText("Lints unavailable")).toBeTruthy();
  });

  it("caption counts match the polyline", async () => {
    const points = Array.from({ length: 1000 }, (_, index) => ({
      open_time: index,
      equity: (100 + (index % 37)).toFixed(2),
      cash: "0",
    }));
    install(result({ equity_curve: points }));
    render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    const count = document.querySelector("polyline")!.getAttribute("points")!.split(" ").length;
    expect(document.querySelector(".equity figcaption")!.textContent).toContain(`${count} of 1000 points drawn`);
  });
});

describe("single equity point", () => {
  it("draws one valid point as a dot", async () => {
    install(result({ equity_curve: [{ open_time: 1, equity: "100.00", cash: "1" }] }));
    render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    expect(document.querySelector("circle")).not.toBeNull();
    expect(document.querySelector("polyline")).toBeNull();
  });
});

describe("paging and load state", () => {
  it("pages 50 at a time and resets for a new result", async () => {
    install((id: string) => jsonResponse(id === ID ? result({}, 120) : result({}, 7)));
    render(<BacktestResults />);
    await load();
    await screen.findByText("1-50 of 120");
    const next = () => screen.getByRole("button", { name: "Next" }) as HTMLButtonElement;
    const previous = () => screen.getByRole("button", { name: "Previous" }) as HTMLButtonElement;
    expect(previous().disabled).toBe(true);
    expect(next().disabled).toBe(false);
    fireEvent.click(next());
    await screen.findByText("51-100 of 120");
    expect(previous().disabled).toBe(false);
    fireEvent.click(next());
    await screen.findByText("101-120 of 120");
    expect(next().disabled).toBe(true);
    expect(document.querySelectorAll(".trade-log tbody tr").length).toBe(20);
    await load(ID2);
    await screen.findByText("1-7 of 7");
  });

  it("enables next only when another page exists", async () => {
    install(result({}, 51));
    let view = render(<BacktestResults />);
    await load();
    await screen.findByText("1-50 of 51");
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(false);
    view.unmount();

    install(result({}, 50));
    view = render(<BacktestResults />);
    await load();
    await screen.findByText("1-50 of 50");
    expect((screen.getByRole("button", { name: "Next" }) as HTMLButtonElement).disabled).toBe(true);
    view.unmount();

    install(result({}, 0));
    view = render(<BacktestResults />);
    await load();
    await screen.findByText("0 trades");
    expect([...document.querySelectorAll(".trade-log th")].map((cell) => cell.textContent)).toEqual([
      ...TRADE_COLUMNS,
    ]);
  });

  it("clears the previous error and result while the next load is pending", async () => {
    let release: (response: Response) => void = () => {};
    let resultsCalls = 0;
    const fetchMock = install((id: string) =>
      id === ID
        ? jsonResponse({ error: { code: "internal_error", message: "boom", path: null } }, 500)
        : jsonResponse(result()),
    );
    render(<BacktestResults />);
    await load();
    expect((await screen.findByRole("alert")).textContent).toBe("internal_error: boom");

    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/results")) {
        resultsCalls += 1;
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      }
      return jsonResponse({ job_id: ID2, status: "succeeded" });
    });
    await load(ID2);
    await waitFor(() => expect(resultsCalls).toBe(1));
    expect((screen.getByRole("button", { name: "Loading…" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("alert")).toBeNull();
    await act(async () => {
      release(jsonResponse(result()));
    });
    await screen.findByText("Metrics");
    expect(screen.queryByRole("alert")).toBeNull();

    resultsCalls = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/results")) {
        resultsCalls += 1;
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      }
      return jsonResponse({ job_id: ID, status: "succeeded" });
    });
    await load(ID);
    await waitFor(() => expect(screen.queryByText("Metrics")).toBeNull());
    await act(async () => {
      release(jsonResponse(result()));
    });
    await screen.findByText("Metrics");
  });

  it("does not fetch an invalid id", async () => {
    const fetchMock = install(result());
    render(<BacktestResults />);
    await load("zz");
    expect((await screen.findByRole("alert")).textContent).toBe("invalid_id: job_id is invalid");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("export downloads", () => {
  it("requests each file at its url with the matching accept and revokes the blob", async () => {
    const seen: { url: string; accept: string | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const accept = new Headers(init?.headers).get("accept");
        seen.push({ url, accept });
        if (url.includes("/exports/")) {
          return new Response("a,b\n1,2\n", { status: 200 });
        }
        if (url.endsWith("/results")) {
          return jsonResponse(result());
        }
        return jsonResponse({ job_id: ID, status: "succeeded" });
      }),
    );
    const names: string[] = [];
    const revoked: string[] = [];
    vi.spyOn(URL, "createObjectURL").mockImplementation(() => "blob:backtest");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation((url) => {
      revoked.push(String(url));
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download);
    });
    render(<BacktestResults />);
    await load();
    await screen.findByText("Metrics");
    const labels = [
      "Download trades.csv",
      "Download trades.json",
      "Download metrics.csv",
      "Download metrics.json",
      "Download result JSON",
    ];
    for (const label of labels) {
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() => expect(names.length).toBe(labels.indexOf(label) + 1));
    }
    expect(names).toEqual(
      ["trades.csv", "trades.json", "metrics.csv", "metrics.json", "result.json"].map(
        (kind) => `backtest-${ID}-${kind}`,
      ),
    );
    expect(revoked).toHaveLength(5);
    const downloads = seen
      .filter((call) => call.url.includes("/exports/"))
      .map((call) => [call.url, call.accept]);
    expect(downloads).toEqual([
      [`/api/backtests/${ID}/exports/trades.csv`, "text/csv"],
      [`/api/backtests/${ID}/exports/trades.json`, "application/json"],
      [`/api/backtests/${ID}/exports/metrics.csv`, "text/csv"],
      [`/api/backtests/${ID}/exports/metrics.json`, "application/json"],
    ]);
  });
});
