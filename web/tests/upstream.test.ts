import { afterEach, describe, expect, it, vi } from "vitest";

import { POST as acceptProposal } from "@/app/api/agent/proposals/[proposalId]/accept/route";
import { POST as streamMessage } from "@/app/api/agent/sessions/[sessionId]/messages/stream/route";
import { POST as createSession } from "@/app/api/agent/sessions/route";
import { GET as listStrategies } from "@/app/api/strategies/route";
import { GET as getStrategy, PUT as putStrategy } from "@/app/api/strategies/[strategyId]/route";
import { authorOrigin, MAX_PROXY_BODY_BYTES, readCappedBody } from "@/lib/upstream";
import { SESSION_ID, STRATEGY_ID } from "@/tests/fixtures";

const ORIGIN = "http://127.0.0.1:8000";

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_ORIGIN;
});

describe("author proxy", () => {
  it("refuses a missing or credentialed origin without calling fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    delete process.env.AGENT_API_ORIGIN;
    const missing = await createSession(new Request("http://chat.local/api/agent/sessions", { method: "POST", body: "{}" }));
    expect(missing.status).toBe(503);
    expect(await missing.json()).toEqual({
      error: {
        code: "service_unavailable",
        message: "the author API is not configured",
        path: null,
      },
    });
    process.env.AGENT_API_ORIGIN = `http://${["user", "pass"].join(":")}@127.0.0.1:8000`;
    expect(authorOrigin()).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards the stream and a 409 without adding a credential header", async () => {
    process.env.AGENT_API_ORIGIN = ORIGIN;
    const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        calls.push({ url, init });
        if (url.endsWith("/messages/stream")) {
          return new Response("id: 1\nevent: user_message\ndata: {}\n\n", {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          });
        }
        return new Response(
          JSON.stringify({
            error: {
              code: "proposal_already_decided",
              message: "proposal is already decided",
              path: null,
            },
          }),
          { status: 409, headers: { "content-type": "application/json" } },
        );
      }),
    );

    const streamed = await streamMessage(
      new Request("http://chat.local/api", {
        method: "POST",
        body: JSON.stringify({ content: "hello" }),
      }),
      { params: Promise.resolve({ sessionId: SESSION_ID }) },
    );
    expect(streamed.status).toBe(200);
    expect(streamed.headers.get("content-type")).toContain("text/event-stream");
    expect(await streamed.text()).toContain("user_message");
    expect(calls[0]?.url).toBe(`${ORIGIN}/agent/sessions/${SESSION_ID}/messages/stream`);
    const headers = new Headers(calls[0]?.init?.headers);
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("accept")).toBe("text/event-stream");
    expect(calls[0]?.init?.body).toBe(JSON.stringify({ content: "hello" }));

    const rejected = await acceptProposal(
      new Request("http://chat.local/api", { method: "POST", body: "{}" }),
      { params: Promise.resolve({ proposalId: "not-an-id" }) },
    );
    expect(rejected.status).toBe(422);
    expect(calls).toHaveLength(1);

    const conflict = await acceptProposal(
      new Request("http://chat.local/api", { method: "POST", body: "{}" }),
      { params: Promise.resolve({ proposalId: "b".repeat(32) }) },
    );
    expect(conflict.status).toBe(409);
    expect(await conflict.json()).toEqual({
      error: {
        code: "proposal_already_decided",
        message: "proposal is already decided",
        path: null,
      },
    });
    expect(calls[1]?.url).toBe(`${ORIGIN}/agent/proposals/${"b".repeat(32)}/accept`);
    expect(new Headers(calls[1]?.init?.headers).get("authorization")).toBeNull();
  });

  it("returns 413 for an oversized body and does not call the author API", async () => {
    process.env.AGENT_API_ORIGIN = ORIGIN;
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const response = await createSession(
      new Request("http://chat.local/api/agent/sessions", {
        method: "POST",
        body: "x".repeat(MAX_PROXY_BODY_BYTES + 1),
      }),
    );
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({
      error: { code: "payload_too_large", message: "request body is too large", path: null },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("cancels the body stream when the next chunk would pass the cap", async () => {
    let pulls = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        if (pulls === 1) {
          controller.enqueue(new Uint8Array(MAX_PROXY_BODY_BYTES));
          return;
        }
        if (pulls === 2) {
          controller.enqueue(new Uint8Array(1));
          return;
        }
        controller.enqueue(new Uint8Array(8 * 1024 * 1024));
        controller.close();
      },
    });
    const request = { headers: new Headers(), body: stream } as Request;
    const response = await readCappedBody(request, MAX_PROXY_BODY_BYTES);
    expect(response).toBeInstanceOf(Response);
    expect((response as Response).status).toBe(413);
    expect(pulls).toBe(2);
  });

  it("forwards a strategy read and save and refuses a bad id", async () => {
    process.env.AGENT_API_ORIGIN = ORIGIN;
    const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        calls.push({ url, init });
        return new Response(JSON.stringify({ strategies: [], total: 0 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
    const listed = await listStrategies(
      new Request("http://chat.local/api/strategies?limit=10&include_archived=true"),
    );
    expect(listed.status).toBe(200);
    expect(calls[0]?.url).toBe(`${ORIGIN}/strategies?limit=10`);
    expect(calls[0]?.init?.method).toBe("GET");

    const rejected = await getStrategy(new Request("http://chat.local/api"), {
      params: Promise.resolve({ strategyId: "not-an-id" }),
    });
    expect(rejected.status).toBe(422);
    expect(calls).toHaveLength(1);

    const hash = "ab".repeat(32);
    const saved = await putStrategy(
      new Request("http://chat.local/api", {
        method: "PUT",
        body: JSON.stringify({ dsl: "strategy x:", expected_version: 2, expected_ir_hash: hash }),
      }),
      { params: Promise.resolve({ strategyId: STRATEGY_ID }) },
    );
    expect(saved.status).toBe(200);
    expect(calls[1]?.url).toBe(`${ORIGIN}/strategies/${STRATEGY_ID}`);
    expect(calls[1]?.init?.method).toBe("PUT");
    expect(calls[1]?.init?.body).toContain("expected_ir_hash");
    expect(new Headers(calls[1]?.init?.headers).get("x-tradoor-client")).toBe("web");
    expect(calls.some((call) => call.url.includes("/tools/"))).toBe(false);
  });

  it("refuses a strategy save that lacks a valid concurrency pair", async () => {
    process.env.AGENT_API_ORIGIN = ORIGIN;
    const hash = "ab".repeat(32);
    const cases: Array<{ body: string; code: string; path: string | null }> = [
      { body: JSON.stringify({ dsl: "strategy x:" }), code: "invalid_request", path: "$" },
      { body: JSON.stringify({ dsl: "strategy x:", expected_version: 1 }), code: "invalid_request", path: "$" },
      {
        body: JSON.stringify({ dsl: "strategy x:", expected_ir_hash: hash }),
        code: "invalid_request",
        path: "$",
      },
      {
        body: JSON.stringify({ expected_version: null, expected_ir_hash: hash }),
        code: "invalid_request",
        path: "$",
      },
      {
        body: JSON.stringify({ expected_version: 1, expected_ir_hash: null }),
        code: "invalid_request",
        path: "$",
      },
      {
        body: JSON.stringify({ expected_version: null, expected_ir_hash: null }),
        code: "invalid_request",
        path: "$",
      },
      {
        body: JSON.stringify({ expected_version: true, expected_ir_hash: hash }),
        code: "invalid_request",
        path: "$.expected_version",
      },
      {
        body: JSON.stringify({ expected_version: "1", expected_ir_hash: hash }),
        code: "invalid_request",
        path: "$.expected_version",
      },
      {
        body: JSON.stringify({ expected_version: 0, expected_ir_hash: hash }),
        code: "invalid_request",
        path: "$.expected_version",
      },
      {
        body: JSON.stringify({ expected_version: 1.5, expected_ir_hash: hash }),
        code: "invalid_request",
        path: "$.expected_version",
      },
      {
        body: JSON.stringify({ expected_version: 1, expected_ir_hash: "ab" }),
        code: "invalid_request",
        path: "$.expected_ir_hash",
      },
      {
        body: JSON.stringify({ expected_version: 1, expected_ir_hash: "AB".repeat(32) }),
        code: "invalid_request",
        path: "$.expected_ir_hash",
      },
      { body: "not-json", code: "invalid_json", path: null },
      { body: "[1]", code: "invalid_json", path: null },
      { body: "null", code: "invalid_json", path: null },
    ];
    for (const item of cases) {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const response = await putStrategy(
        new Request("http://chat.local/api", { method: "PUT", body: item.body }),
        { params: Promise.resolve({ strategyId: STRATEGY_ID }) },
      );
      expect(response.status).toBe(422);
      expect(await response.json()).toEqual({
        error: {
          code: item.code,
          message: expect.any(String),
          path: item.path,
        },
      });
      expect(fetchMock).not.toHaveBeenCalled();
    }

    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const oversized = await putStrategy(
      new Request("http://chat.local/api", {
        method: "PUT",
        body: "x".repeat(MAX_PROXY_BODY_BYTES + 1),
      }),
      { params: Promise.resolve({ strategyId: STRATEGY_ID }) },
    );
    expect(oversized.status).toBe(413);
    expect(await oversized.json()).toEqual({
      error: { code: "payload_too_large", message: "request body is too large", path: null },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards a valid strategy save unchanged", async () => {
    process.env.AGENT_API_ORIGIN = ORIGIN;
    const hash = "cd".repeat(32);
    const raw = `{"dsl":"strategy x:","expected_version":2,"expected_ir_hash":"${hash}"}`;
    const calls: Array<{ url: string; init: RequestInit | undefined }> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
        calls.push({ url, init });
        return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
      }),
    );
    const saved = await putStrategy(new Request("http://chat.local/api", { method: "PUT", body: raw }), {
      params: Promise.resolve({ strategyId: STRATEGY_ID }),
    });
    expect(saved.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.init?.method).toBe("PUT");
    expect(calls[0]?.init?.body).toBe(raw);
    expect(new Headers(calls[0]?.init?.headers).get("x-tradoor-client")).toBe("web");
  });
});
