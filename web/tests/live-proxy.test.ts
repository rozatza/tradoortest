import { afterEach, describe, expect, it, vi } from "vitest";

import { POST as confirm } from "@/app/api/live/confirm/route";
import { POST as flatten } from "@/app/api/live/flatten/route";
import { POST as prepare } from "@/app/api/live/prepare/route";
import { forwardLive } from "@/lib/upstream";

const TOKEN = "loopback-token";

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_ORIGIN;
  delete process.env.LIVE_CONTROL_TOKEN;
});

function stub() {
  process.env.AGENT_API_ORIGIN = "http://127.0.0.1:8000";
  process.env.LIVE_CONTROL_TOKEN = TOKEN;
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init: init ?? {} });
      return new Response(JSON.stringify({ accepted: true, sent: false }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

describe("live control proxy", () => {
  it("sets the loopback origin and the server token for an allowlisted Origin", async () => {
    const calls = stub();
    const response = await prepare(
      new Request("http://chat.local/api/live/prepare", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          origin: "http://localhost:3000",
          "x-live-token": "from-the-browser",
          cookie: "a=b",
          authorization: "Bearer z",
        },
        body: JSON.stringify({ side: "buy", action: "entry", mint: "SOL", amount: 1 }),
      }),
    );
    expect(response.status).toBe(200);
    const headers = new Headers(calls[0]?.init.headers);
    expect(headers.get("origin")).toBe("http://127.0.0.1");
    expect(headers.get("x-live-token")).toBe(TOKEN);
    expect(headers.get("x-live-token")).not.toBe("from-the-browser");
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("authorization")).toBeNull();
    expect(calls[0]?.url).toBe("http://127.0.0.1:8000/live/prepare");
    const text = await response.text();
    expect(text).not.toContain(TOKEN);
  });

  it("rejects a cross-site Origin before the control token is attached", async () => {
    const calls = stub();
    const evil = {
      "content-type": "application/json",
      origin: "https://evil.example",
      "x-live-token": "from-the-browser",
    };
    const prepareResponse = await prepare(
      new Request("http://chat.local/api/live/prepare", {
        method: "POST",
        headers: evil,
        body: JSON.stringify({ side: "buy", action: "entry", mint: "SOL", amount: 1 }),
      }),
    );
    const flattenResponse = await flatten(
      new Request("http://chat.local/api/live/flatten", {
        method: "POST",
        headers: evil,
        body: JSON.stringify({ side: "sell", action: "flatten", mint: "SOL", amount: 1 }),
      }),
    );
    const confirmResponse = await confirm(
      new Request("http://chat.local/api/live/confirm", {
        method: "POST",
        headers: { origin: "null" },
        body: JSON.stringify({ pending_id: 1, signed_transaction_b64: "AA==" }),
      }),
    );
    const missing = await forwardLive(
      "/live/prepare",
      new Request("http://chat.local/api/live/prepare", {
        method: "POST",
        body: JSON.stringify({ side: "buy", action: "entry", mint: "SOL", amount: 1 }),
      }),
    );
    for (const response of [prepareResponse, flattenResponse, confirmResponse, missing]) {
      expect(response.status).toBe(403);
      const text = await response.text();
      expect(text).not.toContain(TOKEN);
      expect(text).toContain("control_forbidden");
    }
    expect(calls).toHaveLength(0);
  });

  it("fails closed when the token is unset and does not call the service", async () => {
    const calls = stub();
    delete process.env.LIVE_CONTROL_TOKEN;
    const response = await confirm(
      new Request("http://chat.local/api/live/confirm", {
        method: "POST",
        headers: { origin: "http://127.0.0.1" },
        body: JSON.stringify({ pending_id: 1, signed_transaction_b64: "AA==" }),
      }),
    );
    expect(response.status).toBe(503);
    expect(calls).toHaveLength(0);
  });

  it("does not proxy an unknown live path", async () => {
    const calls = stub();
    const response = await forwardLive(
      "/live/kill",
      new Request("http://chat.local/api/live/kill", { method: "POST", body: "{}" }),
    );
    expect(response.status).toBe(404);
    expect(calls).toHaveLength(0);
  });
});
