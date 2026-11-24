import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PaperBook, PaperDashboard } from "@/components/paper-dashboard";
import { paperPnl, readSnapshot } from "@/lib/paper";

const ID = "a".repeat(32);

function payload(state: "open" | "open_unmanaged", realized: string, kind: string) {
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
    events: [
      {
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
    const first = payload("open", "1/2", "ORDER_FILLED");
    const second = payload("open_unmanaged", "4/1", "BAR_MISSED");
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        calls += 1;
        if (calls > 1) {
          await gate;
        }
        return Response.json(calls === 1 ? first : second);
      }),
    );
    render(<PaperDashboard pollMs={20} />);
    expect(await screen.findByText("Hand")).toBeTruthy();
    expect(screen.getByText("running")).toBeTruthy();
    expect(screen.getByText("open")).toBeTruthy();
    expect(screen.getByText(/Stored realized 1\/2/)).toBeTruthy();
    expect(screen.getByText(/Mark 60/)).toBeTruthy();
    expect(screen.getByText(/stored realized \+ mark 41\/2/)).toBeTruthy();
    expect(screen.getByText("ORDER_FILLED")).toBeTruthy();
    expect(screen.getByText("PAPER / simulated. No live order is sent.")).toBeTruthy();
    expect(document.body.textContent).not.toContain("999");
    release();
    expect(await screen.findByText("open_unmanaged")).toBeTruthy();
    expect(screen.getByText("BAR_MISSED")).toBeTruthy();
    expect(screen.getByText('{"reason":"ttl"}')).toBeTruthy();
    expect(screen.getByText(/Stored realized 4\/1/)).toBeTruthy();
    expect(screen.queryByText("ORDER_FILLED")).toBeNull();
    expect(screen.queryByText("running")).toBeNull();
    expect(document.body.textContent).not.toContain("999");
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
        if (url.endsWith("/api/paper")) {
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
    expect(screen.getByText(/Stored realized 4\/1/)).toBeTruthy();
    const pnl = paperPnl(snapshot.positions[0]!);
    expect(pnl.combinedLabel).toBe("stored realized + mark");
    expect(screen.getByText(`stored realized + mark ${pnl.combined}`)).toBeTruthy();
    expect(document.body.textContent).not.toContain("999");
  });
});
