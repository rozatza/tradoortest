import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PaperBook, PaperDashboard } from "@/components/paper-dashboard";
import { paperPnl, readSnapshot } from "@/lib/paper";

const ID = "a".repeat(32);

function payload(
  state: "open" | "open_unmanaged",
  realized: string,
  kind: string,
  eventId = 4,
) {
  return {
    mode: "paper",
    simulated: true,
    cash: "999",
    strategies:
      state === "open"
        ? [
            {
              strategy_id: ID,
              version: 1,
              name: "Hand",
              bar_size: "1m",
              registered_at: 1_700_000_040,
              status: "running",
            },
          ]
        : [],
    positions: [
      {
        strategy_id: ID,
        version: 1,
        asset_id: "sol",
        state,
        qty: "2",
        open_cost: "100",
        entry: "50",
        basis: "100/1",
        realized,
        trade_realized: null,
        take_profit: null,
        stop_loss: null,
        anchor: null,
        attached_at: 1_700_000_040,
        mark: {
          label: "mark",
          price: "60",
          open_time: 1_700_000_040,
          timeframe: "1m",
          source: "last_stored_close",
        },
      },
    ],
    events_cursor: eventId,
    events_has_more: false,
    drift_threshold: "0.01",
    drift_threshold_name: "PAPER_DRIFT_THRESHOLD",
    drift: [],
    events: [
      {
        id: eventId,
        strategy_id: ID,
        version: 1,
        open_time: 1_700_000_100,
        kind,
        detail: kind === "BAR_MISSED" ? '{"reason":"ttl"}' : '{"side":"buy"}',
      },
    ],
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("paper dashboard", () => {
  it("shows the fixture and replaces it on the next poll", async () => {
    const first = payload("open", "1/2", "ORDER_FILLED", 4);
    const second = payload("open_unmanaged", "4/1", "BAR_MISSED", 5);
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const urls: string[] = [];
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        urls.push(url);
        calls += 1;
        if (calls === 1) {
          return Response.json(first);
        }
        if (calls === 2) {
          await gate;
        }
        return Response.json(second);
      }),
    );
    render(<PaperDashboard pollMs={20} />);
    expect(await screen.findByText("Hand")).toBeTruthy();
    expect(urls[0]).toContain("events_limit=100");
    expect(urls[0]).not.toContain("events_since=");
    expect(screen.getByText("running")).toBeTruthy();
    expect(screen.getByText("open")).toBeTruthy();
    expect(screen.getByText(/Stored realized 0\.50/)).toBeTruthy();
    expect(screen.getByText(/Mark 60/)).toBeTruthy();
    expect(screen.getByText(/stored realized \+ mark 20\.50/)).toBeTruthy();
    expect(screen.getByText("ORDER_FILLED")).toBeTruthy();
    expect(screen.getByText("PAPER / simulated. No live order is sent.")).toBeTruthy();
    expect(document.body.textContent).not.toContain("999");
    expect(document.body.textContent).not.toContain("1/2");
    release();
    expect(await screen.findByText("open_unmanaged")).toBeTruthy();
    expect(urls.some((url) => url.includes("events_since=4"))).toBe(true);
    expect(screen.getByText("BAR_MISSED")).toBeTruthy();
    expect(screen.getByText("ORDER_FILLED")).toBeTruthy();
    expect(screen.getByText('{"reason":"ttl"}')).toBeTruthy();
    expect(screen.getByText(/Stored realized 4\.00/)).toBeTruthy();
    expect(screen.queryByText("running")).toBeNull();
    expect(document.body.textContent).not.toContain("999");
    expect(document.body.textContent).not.toContain("4/1");
  });

  it("deploys and undeploys through the paper proxy", async () => {
    const calls: Array<{ url: string; method: string; body: string | null }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({
          url,
          method: init?.method ?? "GET",
          body: typeof init?.body === "string" ? init.body : null,
        });
        if (url.includes("/api/paper")) {
          return Response.json(payload("open", "1/2", "ORDER_FILLED"));
        }
        return Response.json({ status: "running" });
      }),
    );
    render(<PaperDashboard pollMs={60_000} />);
    expect(await screen.findByRole("button", { name: `Undeploy ${ID} version 1` })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: `Undeploy ${ID} version 1` }));
    await waitFor(() => {
      expect(calls.some((call) => call.method === "DELETE" && call.url.endsWith("/versions/1/paper"))).toBe(
        true,
      );
    });
    fireEvent.change(screen.getByLabelText("Strategy id"), { target: { value: "not-an-id" } });
    fireEvent.click(screen.getByRole("button", { name: "Deploy on PAPER" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
    fireEvent.change(screen.getByLabelText("Strategy id"), { target: { value: ID } });
    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "2" } });
    fireEvent.click(screen.getByRole("button", { name: "Deploy on PAPER" }));
    await waitFor(() => {
      expect(calls.some((call) => call.method === "POST")).toBe(true);
    });
    const posted = calls.find((call) => call.method === "POST");
    expect(posted?.url).toBe(`/api/strategies/${ID}/versions/2/paper`);
    expect(posted?.body).toBe("{}");
  });

  it("renders an unmanaged book from the parsed snapshot", () => {
    const snapshot = readSnapshot(payload("open_unmanaged", "4/1", "BAR_MISSED"));
    render(<PaperBook snapshot={snapshot} />);
    expect(screen.getByText("open_unmanaged")).toBeTruthy();
    expect(screen.getByText("BAR_MISSED")).toBeTruthy();
    expect(screen.getByText(/Stored realized 4\.00/)).toBeTruthy();
    const pnl = paperPnl(snapshot.positions[0]!);
    expect(pnl.combined).toBe("24/1");
    expect(pnl.combinedLabel).toBe("stored realized + mark");
    expect(screen.getByText("stored realized + mark 24.00")).toBeTruthy();
    expect(document.body.textContent).not.toContain("999");
    expect(document.body.textContent).not.toContain("4/1");
  });

  it("renders a fraction as a decimal and flags a drift row", () => {
    const snapshot = readSnapshot({
      ...payload("open", "-250749/50", "ORDER_FILLED"),
      drift: [
        {
          strategy_id: ID,
          version: 1,
          status: "drift",
          flagged: true,
          reason: null,
          threshold: "0.01",
          fill_count: { paper: 1, backtest: 1 },
          side_time_mismatches: 0,
          price_delta: "1.50",
          realized_pnl: { paper: "-5014.98", backtest: "0.00", delta: "-5014.98" },
          costs: { commission_bps: "10", slippage_bps: "5" },
        },
      ],
    });
    render(<PaperBook snapshot={snapshot} />);
    expect(screen.getByText("Stored realized -5014.98")).toBeTruthy();
    expect(document.body.textContent).not.toContain("-250749/50");
    expect(screen.getByText("flagged")).toBeTruthy();
    expect(screen.getByText("drift")).toBeTruthy();
    expect(screen.getByText(/delta -5014.98/)).toBeTruthy();
    expect(screen.getByText("1 / 1")).toBeTruthy();
    expect(screen.getByText("commission 10 bps, slippage 5 bps")).toBeTruthy();
  });

  it("shows insufficient data and does not flag it", () => {
    const snapshot = readSnapshot({
      ...payload("open", "1/2", "BAR_MISSED"),
      drift: [
        {
          strategy_id: ID,
          version: 1,
          status: "insufficient_data",
          flagged: false,
          reason: "bar_missed",
          threshold: "0.01",
          fill_count: null,
          side_time_mismatches: null,
          price_delta: null,
          realized_pnl: null,
          costs: { commission_bps: "10", slippage_bps: "5" },
        },
      ],
    });
    render(<PaperBook snapshot={snapshot} />);
    expect(screen.getByText("insufficient data (BAR_MISSED)")).toBeTruthy();
    expect(screen.getByText("clear")).toBeTruthy();
    expect(screen.queryByText("flagged")).toBeNull();
    expect(screen.queryByText("match")).toBeNull();
  });

  it("shows insufficient data when the comparison window is missing", () => {
    const snapshot = readSnapshot({
      ...payload("open", "1/2", "ORDER_FILLED"),
      drift: [
        {
          strategy_id: ID,
          version: 1,
          status: "insufficient_data",
          flagged: false,
          reason: "no_window",
          threshold: "0.01",
          fill_count: null,
          side_time_mismatches: null,
          price_delta: null,
          realized_pnl: null,
          costs: { commission_bps: "10", slippage_bps: "5" },
        },
      ],
    });
    render(<PaperBook snapshot={snapshot} />);
    expect(screen.getByText("insufficient data (no comparison window)")).toBeTruthy();
    expect(screen.queryByText("match")).toBeNull();
    expect(screen.queryByText("0 / 0")).toBeNull();
    expect(screen.queryByText("flagged")).toBeNull();
  });
});
