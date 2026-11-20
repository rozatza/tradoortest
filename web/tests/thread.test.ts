import { describe, expect, it } from "vitest";

import { buildThread, insertEvent, stripMarketData } from "@/lib/thread";
import { agentEvent, PROPOSAL_ID, STRATEGY_ID } from "@/tests/fixtures";

describe("buildThread", () => {
  it("keeps tool steps in seq order and marks each one done when it completes", () => {
    const late = agentEvent(5, "tool_complete", {
      tool: "write_strategy",
      call_id: "c2",
      ok: true,
      output: {
        ok: true,
        tool: "write_strategy",
        data: {
          strategy_id: STRATEGY_ID,
          version: 1,
          name: "EMA cross",
          ir_hash: "ab",
          bars: [{ close: 1 }],
        },
      },
    });
    const shuffled = [
      late,
      agentEvent(2, "tool_start", { tool: "validate", call_id: "c1", arguments: {} }),
      agentEvent(1, "user_message", { role: "user", content: "draft" }),
      agentEvent(3, "tool_complete", {
        tool: "validate",
        call_id: "c1",
        ok: false,
        output: { ok: false, error: { message: "strategy is not valid" } },
      }),
      agentEvent(4, "tool_start", { tool: "write_strategy", call_id: "c2", arguments: {} }),
    ];
    const ordered = shuffled.reduce(insertEvent, []);
    expect(ordered.map((event) => event.seq)).toEqual([1, 2, 3, 4, 5]);
    const again = insertEvent(ordered, late);
    expect(again).toBe(ordered);
    const rows = buildThread(ordered);
    const tools = rows.filter((row) => row.type === "tool");
    expect(tools.map((row) => (row.type === "tool" ? row.tool.tool : ""))).toEqual([
      "validate",
      "write_strategy",
    ]);
    expect(tools.map((row) => (row.type === "tool" ? row.tool.state : ""))).toEqual([
      "done",
      "done",
    ]);
    expect(tools[0]?.type === "tool" ? tools[0].tool.errorMessage : null).toBe(
      "strategy is not valid",
    );
    const strategy = rows.find((row) => row.type === "strategy");
    expect(strategy?.type === "strategy" ? strategy.strategy.action : null).toBe("written");
    expect(strategy?.type === "strategy" ? strategy.strategy.name : null).toBe("EMA cross");
    expect(JSON.stringify(ordered)).not.toContain("bars");
    expect(stripMarketData({ bars: [1], name: "keep" })).toEqual({ name: "keep" });
  });

  it("shows a strategy card when an accepted proposal updates a strategy", () => {
    const rows = buildThread([
      agentEvent(1, "proposal_created", {
        proposal_id: PROPOSAL_ID,
        strategy_id: STRATEGY_ID,
        from_version: 1,
        to_version: 2,
        status: "pending",
        dsl_diff: "--- a\n+++ b\n",
        ir_diff: [],
      }),
      agentEvent(2, "proposal_decided", {
        proposal_id: PROPOSAL_ID,
        decision: "accepted",
        status: "accepted",
        strategy_id: STRATEGY_ID,
        applied_version: 2,
        applied_ir_hash: "bb",
      }),
    ]);
    const proposal = rows.find((row) => row.type === "proposal");
    const strategy = rows.find((row) => row.type === "strategy");
    expect(proposal?.type === "proposal" ? proposal.proposal.status : null).toBe("accepted");
    expect(strategy?.type === "strategy" ? strategy.strategy.action : null).toBe("updated");
    expect(strategy?.type === "strategy" ? strategy.strategy.version : null).toBe(2);
  });

  it("keeps the persisted worker error visible without repeating turn_complete", () => {
    const rows = buildThread([
      agentEvent(1, "user_message", { role: "user", content: "boom" }),
      agentEvent(2, "error", {
        code: "internal_error",
        message: "the request could not be completed",
        path: null,
      }),
      agentEvent(3, "turn_complete", {
        status: "error",
        turn_id: "d".repeat(32),
        error: {
          code: "internal_error",
          message: "the request could not be completed",
          path: null,
        },
      }),
    ]);
    const errors = rows.filter((row) => row.type === "error");
    expect(errors).toHaveLength(1);
    expect(errors[0]?.type === "error" ? errors[0].error.code : null).toBe("internal_error");
  });

  it("stops only the tool steps that the stream abandoned", () => {
    const rows = buildThread([
      agentEvent(1, "user_message", { role: "user", content: "hi" }),
      agentEvent(2, "tool_start", { tool: "validate", call_id: "c1", arguments: {} }),
      agentEvent(3, "stream_incomplete", {
        code: "stream_incomplete",
        message: "the response ended before the turn finished",
      }),
      agentEvent(4, "tool_start", { tool: "write_strategy", call_id: "c2", arguments: {} }),
    ]);
    const tools = rows.filter((row) => row.type === "tool");
    expect(tools.map((row) => (row.type === "tool" ? row.tool.state : ""))).toEqual([
      "stopped",
      "working",
    ]);
    const error = rows.find((row) => row.type === "error");
    expect(error?.type === "error" ? error.error.code : null).toBe("stream_incomplete");
    expect(error?.type === "error" ? error.error.message : null).toContain(
      "ended before the turn finished",
    );
  });

  it("stops every in-progress tool step when the stream aborts", () => {
    const rows = buildThread([
      agentEvent(1, "tool_start", { tool: "validate", call_id: "c1", arguments: {} }),
      agentEvent(2, "tool_complete", {
        tool: "read_strategy",
        call_id: "c0",
        ok: true,
        output: { ok: true },
      }),
      agentEvent(3, "tool_start", { tool: "write_strategy", call_id: "c2", arguments: {} }),
      agentEvent(4, "stream_aborted", {}),
    ]);
    const tools = rows.filter((row) => row.type === "tool");
    expect(tools.map((row) => (row.type === "tool" ? `${row.tool.tool}:${row.tool.state}` : ""))).toEqual([
      "validate:stopped",
      "read_strategy:done",
      "write_strategy:stopped",
    ]);
    expect(rows.some((row) => row.type === "error")).toBe(false);
  });
});
