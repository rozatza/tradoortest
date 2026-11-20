import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

import { afterEach, describe, expect, it, vi } from "vitest";

import { GET as getAnalysis } from "@/app/api/backtests/[jobId]/analysis/route";
import { GET as getBars } from "@/app/api/backtests/[jobId]/bars/route";
import { GET as getIndicators } from "@/app/api/backtests/[jobId]/indicators/route";
import { GET as getJob } from "@/app/api/backtests/[jobId]/route";
import { GET as getMetricsCsv } from "@/app/api/backtests/[jobId]/exports/metrics.csv/route";
import { GET as getMetricsJson } from "@/app/api/backtests/[jobId]/exports/metrics.json/route";
import { GET as getTradesCsv } from "@/app/api/backtests/[jobId]/exports/trades.csv/route";
import { GET as getTradesJson } from "@/app/api/backtests/[jobId]/exports/trades.json/route";
import { GET as getResults } from "@/app/api/backtests/[jobId]/results/route";
import { BACKTEST_READS, MAX_PROXY_BODY_BYTES, backtestPath } from "@/lib/upstream";

const ORIGIN = "http://127.0.0.1:8000";
const JOB_ID = "ab".repeat(16);

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_ORIGIN;
});

describe("backtest proxy allowlist", () => {
  it("lists only the job, result, and export reads", () => {
    expect([...BACKTEST_READS]).toEqual([
      "/backtests/{job_id}",
      "/backtests/{job_id}/results",
      "/backtests/{job_id}/exports/trades.json",
      "/backtests/{job_id}/exports/trades.csv",
      "/backtests/{job_id}/exports/metrics.json",
      "/backtests/{job_id}/exports/metrics.csv",
      "/backtests/{job_id}/bars",
      "/backtests/{job_id}/indicators",
      "/backtests/{job_id}/analysis",
    ]);
    expect(BACKTEST_READS.join("\n")).not.toContain("/tools");
    expect(MAX_PROXY_BODY_BYTES).toBe(1_048_576);
    const root = path.resolve(import.meta.dirname, "../app/api/backtests");
    const files = sourceFiles(root).filter((file) => file.endsWith("route.ts"));
    expect(files.length).toBe(BACKTEST_READS.length);
    const text = files.map((file) => readFileSync(file, "utf8")).join("\n");
    const used = text.match(/\/backtests\/\{job_id\}[^"`\n]*/g) ?? [];
    expect(new Set(used)).toEqual(new Set(BACKTEST_READS));
    expect(text).not.toContain("/tools");
    expect(text).not.toContain("readCappedBody");
    expect(text).not.toContain("NEXT_PUBLIC_");
  });

  it("builds an exact path and refuses anything else", () => {
    expect(backtestPath(JOB_ID, "/backtests/{job_id}")).toBe(`/backtests/${JOB_ID}`);
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/results")).toBe(`/backtests/${JOB_ID}/results`);
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/exports/trades.csv")).toBe(
      `/backtests/${JOB_ID}/exports/trades.csv`,
    );
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/bars")).toBe(`/backtests/${JOB_ID}/bars`);
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/indicators")).toBe(
      `/backtests/${JOB_ID}/indicators`,
    );
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/analysis")).toBe(`/backtests/${JOB_ID}/analysis`);
    for (const id of ["", "A".repeat(32), "g".repeat(32), "a".repeat(31), "a".repeat(33), "../tools/x", `${JOB_ID}/tools`]) {
      expect(backtestPath(id, "/backtests/{job_id}/results"), id).toBeNull();
    }
    expect(backtestPath(JOB_ID, "/tools/backtest_get")).toBeNull();
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/results?limit=1")).toBeNull();
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/exports/trades.txt")).toBeNull();
    expect(backtestPath(JOB_ID, "/backtests/{job_id}/exports/../tools/x")).toBeNull();
  });

  it("forwards the allowlisted reads unchanged and drops a bad id", async () => {
    process.env.AGENT_API_ORIGIN = ORIGIN;
    const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        calls.push({ url, init });
        const csv = url.endsWith(".csv");
        return new Response(csv ? "trade_id\nstored\n" : '{"final_equity" : "11480.52"}', {
          status: 200,
          headers: { "content-type": csv ? "text/csv; charset=utf-8" : "application/json" },
        });
      }),
    );
    const context = { params: Promise.resolve({ jobId: JOB_ID }) };
    const request = new Request(`http://chat.local/api/backtests/${JOB_ID}?tool=/tools/x`, {
      headers: { authorization: "Bearer secret", cookie: "a=b" },
    });
    const handlers = [
      { get: getJob, path: `/backtests/${JOB_ID}`, accept: "application/json" },
      { get: getResults, path: `/backtests/${JOB_ID}/results`, accept: "application/json" },
      { get: getTradesJson, path: `/backtests/${JOB_ID}/exports/trades.json`, accept: "application/json" },
      { get: getTradesCsv, path: `/backtests/${JOB_ID}/exports/trades.csv`, accept: "text/csv" },
      { get: getMetricsJson, path: `/backtests/${JOB_ID}/exports/metrics.json`, accept: "application/json" },
      { get: getMetricsCsv, path: `/backtests/${JOB_ID}/exports/metrics.csv`, accept: "text/csv" },
      { get: getBars, path: `/backtests/${JOB_ID}/bars`, accept: "application/json" },
      { get: getIndicators, path: `/backtests/${JOB_ID}/indicators`, accept: "application/json" },
      { get: getAnalysis, path: `/backtests/${JOB_ID}/analysis`, accept: "application/json" },
    ];
    for (const handler of handlers) {
      const response = await handler.get(request, context);
      expect(response.status).toBe(200);
      expect(response.headers.get("cache-control")).toBe("no-store");
      const body = await response.text();
      if (handler.accept === "text/csv") {
        expect(body).toBe("trade_id\nstored\n");
        expect(response.headers.get("content-type")).toContain("text/csv");
      } else {
        expect(body).toBe('{"final_equity" : "11480.52"}');
      }
    }
    expect(calls.map((call) => call.url)).toEqual(handlers.map((handler) => `${ORIGIN}${handler.path}`));
    for (const call of calls) {
      const headers = new Headers(call.init?.headers);
      expect(headers.get("authorization")).toBeNull();
      expect(headers.get("cookie")).toBeNull();
      expect(call.url).not.toContain("?");
      expect(call.url).not.toContain("/tools/");
      expect(call.init?.method).toBe("GET");
      expect(call.init?.cache).toBe("no-store");
    }
    expect(new Headers(calls[3]?.init?.headers).get("accept")).toBe("text/csv");

    const rejected = await getResults(request, { params: Promise.resolve({ jobId: "not-a-job" }) });
    expect(rejected.status).toBe(422);
    expect(await rejected.json()).toEqual({
      error: { code: "invalid_id", message: "job_id is invalid", path: null },
    });
    expect(calls).toHaveLength(handlers.length);
  });
});

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      found.push(...sourceFiles(full));
      continue;
    }
    found.push(full);
  }
  return found;
}
