import { describe, expect, it } from "vitest";

import {
  formatPaperAmount,
  paperDeployPath,
  paperPnl,
  readSnapshot,
  type PaperPosition,
} from "@/lib/paper";

const ID = "a".repeat(32);

function position(overrides: Partial<PaperPosition> = {}): PaperPosition {
  return {
    strategyId: ID,
    version: 1,
    assetId: "sol",
    state: "open",
    qty: "2",
    entry: "50",
    basis: "100/1",
    realized: "1/2",
    mark: {
      label: "mark",
      price: "60",
      openTime: 1_700_000_040,
      timeframe: "1m",
      source: "last_stored_close",
    },
    ...overrides,
  };
}

function body(positionState: PaperPosition["state"] = "open") {
  return {
    mode: "paper",
    simulated: true,
    cash: "999",
    wallet: { address: "hidden" },
    strategies: [
      {
        strategy_id: ID,
        version: 1,
        name: "Hand",
        bar_size: "1m",
        registered_at: 1_700_000_040,
        status: "running",
      },
    ],
    positions: [
      {
        strategy_id: ID,
        version: 1,
        asset_id: "sol",
        state: positionState,
        qty: positionState === "flat" ? null : "2",
        open_cost: "100",
        entry: positionState === "flat" ? null : "50",
        basis: positionState === "flat" ? null : "100/1",
        realized: "1/2",
        trade_realized: "0/1",
        take_profit: null,
        stop_loss: null,
        anchor: null,
        attached_at: 1_700_000_040,
        pnl: "999",
        equity: "999",
        mark:
          positionState === "flat"
            ? null
            : {
                label: "mark",
                price: "60",
                open_time: 1_700_000_040,
                timeframe: "1m",
                source: "last_stored_close",
              },
      },
    ],
    events_cursor: 7,
    events_has_more: false,
    drift_threshold: "0.01",
    drift_threshold_name: "PAPER_DRIFT_THRESHOLD",
    drift: [],
    events: [
      {
        id: 7,
        strategy_id: ID,
        version: 1,
        open_time: 1_700_000_100,
        kind: "BAR_MISSED",
        detail: '{"reason":"ttl"}',
      },
    ],
  };
}

describe("paper snapshot", () => {
  it("keeps stored realized and a labeled mark, and drops every other money field", () => {
    const snapshot = readSnapshot(body());
    expect(snapshot.mode).toBe("paper");
    expect(snapshot.simulated).toBe(true);
    expect(snapshot.strategies[0]).toEqual({
      strategyId: ID,
      version: 1,
      name: "Hand",
      barSize: "1m",
      registeredAt: 1_700_000_040,
      status: "running",
    });
    expect(snapshot.positions[0]?.state).toBe("open");
    expect(snapshot.positions[0]?.realized).toBe("1/2");
    expect(snapshot.positions[0]?.mark?.label).toBe("mark");
    expect(snapshot.positions[0]?.mark?.price).toBe("60");
    expect(JSON.stringify(snapshot)).not.toContain("999");
    expect(JSON.stringify(snapshot)).not.toContain("wallet");
    expect(JSON.stringify(snapshot)).not.toContain("cash");
    expect(snapshot.events[0]?.kind).toBe("BAR_MISSED");
    expect(snapshot.events[0]?.id).toBe(7);
    expect(snapshot.eventsCursor).toBe(7);
    expect(snapshot.driftThresholdName).toBe("PAPER_DRIFT_THRESHOLD");
    expect(snapshot.events[0]?.detail).toBe('{"reason":"ttl"}');
  });

  it("renders a stored fraction as a decimal", () => {
    expect(formatPaperAmount("-250749/50")).toBe("-5014.98");
    expect(formatPaperAmount("1/2")).toBe("0.50");
    expect(formatPaperAmount("41/2")).toBe("20.50");
    expect(formatPaperAmount("4/1")).toBe("4.00");
    expect(formatPaperAmount("1/3")).toBe("0.33333333");
    expect(formatPaperAmount("0/1")).toBe("0.00");
  });

  it("prices an open book as stored realized plus qty times the mark minus basis", () => {
    expect(paperPnl(position())).toEqual({
      realized: "1/2",
      markPrice: "60",
      combined: "41/2",
      combinedLabel: "stored realized + mark",
    });
    expect(paperPnl(position({ state: "open_unmanaged" })).combined).toBe("41/2");
    const flat = paperPnl(position({ state: "flat", qty: null, basis: null, mark: null }));
    expect(flat).toEqual({
      realized: "1/2",
      markPrice: null,
      combined: null,
      combinedLabel: null,
    });
    expect(paperPnl(position({ mark: null })).combined).toBeNull();
  });

  it("builds only a paper deploy path", () => {
    expect(paperDeployPath(ID, "1")).toBe(`/api/strategies/${ID}/versions/1/paper`);
    expect(paperDeployPath("NOPE", "1")).toBeNull();
    expect(paperDeployPath(ID, "0")).toBeNull();
    expect(paperDeployPath(ID, "01")).toBeNull();
  });
});
