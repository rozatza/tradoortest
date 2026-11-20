import { afterEach, describe, expect, it, vi } from "vitest";

import { PUT as putStrategy } from "@/app/api/strategies/[strategyId]/route";
import { MAX_PROXY_BODY_BYTES, readCappedBody, strategySaveError } from "@/lib/upstream";
import { STRATEGY_ID } from "@/tests/fixtures";

const H = "a".repeat(64);

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_ORIGIN;
});

function ok(extra: Record<string, unknown> = {}): string {
  return JSON.stringify({ dsl: "x", expected_version: 1, expected_ir_hash: H, ...extra });
}

function stub() {
  process.env.AGENT_API_ORIGIN = "http://127.0.0.1:8000";
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init: init ?? {} });
      return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
  return calls;
}

async function put(body: BodyInit, headers: Record<string, string> = {}) {
  return putStrategy(
    new Request(`http://chat.local/api/strategies/${STRATEGY_ID}`, {
      method: "PUT",
      body,
      headers: { "content-type": "application/json", ...headers },
    }),
    { params: Promise.resolve({ strategyId: STRATEGY_ID }) },
  );
}

describe("strategy save proxy guard", () => {
  it("rejects a missing, one-sided, or malformed concurrency pair", async () => {
    const calls = stub();
    const rows = [
      "{x",
      "[]",
      '"s"',
      "null",
      "5",
      "",
      '{"dsl":"x"}',
      '{"dsl":"x","expected_version":1}',
      `{"dsl":"x","expected_ir_hash":"${H}"}`,
      '{"expected_version":null,"expected_ir_hash":null}',
      `{"expected_version":"1","expected_ir_hash":"${H}"}`,
      `{"expected_version":true,"expected_ir_hash":"${H}"}`,
      `{"expected_version":0,"expected_ir_hash":"${H}"}`,
      `{"expected_version":-1,"expected_ir_hash":"${H}"}`,
      `{"expected_version":1.5,"expected_ir_hash":"${H}"}`,
      `{"expected_version":1000001,"expected_ir_hash":"${H}"}`,
      `{"expected_version":100000000000000000000000,"expected_ir_hash":"${H}"}`,
      `{"expected_version":[1],"expected_ir_hash":"${H}"}`,
      `{"expected_version":1,"expected_ir_hash":"${H.toUpperCase()}"}`,
      `{"expected_version":1,"expected_ir_hash":"${H.slice(1)}"}`,
      `{"expected_version":1,"expected_ir_hash":"${H}0"}`,
      `{"expected_version":1,"expected_ir_hash":"${H}\\n"}`,
      `{"expected_version":1,"expected_ir_hash":"x${H}"}`,
      `{"expected_version":1,"expected_ir_hash":"${"g".repeat(64)}"}`,
      `{"expected_version":1,"expected_ir_hash":${JSON.stringify(1)}}`,
      '{"expected_version":1,"expected_ir_hash":{}}',
      `{"expected_version":1,"expected_ir_hash":["${H}"]}`,
      `{"a":{"expected_version":1,"expected_ir_hash":"${H}"}}`,
    ];
    for (const row of rows) {
      expect(strategySaveError(row), row).not.toBeNull();
      const response = await put(row);
      expect(response.status, row).toBe(422);
    }
    expect(calls).toHaveLength(0);
    expect(strategySaveError(ok())).toBeNull();
    expect(strategySaveError(JSON.stringify({ expected_version: 1_000_000, expected_ir_hash: H }))).toBeNull();
    expect(
      strategySaveError(JSON.stringify({ expected_version: 1, expected_ir_hash: "0123456789abcdef".repeat(4) })),
    ).toBeNull();
  });

  it("overrides a hostile client header and drops credentials", async () => {
    const calls = stub();
    const body = ok();
    const response = await put(body, {
      "x-tradoor-client": "evil",
      "X-Tradoor-Client": "foo, web",
      cookie: "a=b",
      authorization: "Bearer z",
      host: "evil.example",
    });
    expect(response.status).toBe(200);
    const headers = new Headers(calls[0]?.init.headers);
    expect(headers.get("x-tradoor-client")).toBe("web");
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("authorization")).toBeNull();
    expect(calls[0]?.init.method).toBe("PUT");
    expect(calls[0]?.init.body).toBe(body);
  });

  it("caps a declared body, a stream, and an exact-limit read", async () => {
    const calls = stub();
    expect((await put("a".repeat(MAX_PROXY_BODY_BYTES + 1))).status).toBe(413);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const encoded = new TextEncoder().encode("a".repeat(MAX_PROXY_BODY_BYTES / 2));
        for (let index = 0; index < 3; index += 1) {
          controller.enqueue(encoded);
        }
        controller.close();
      },
    });
    const streamed = await readCappedBody({ headers: new Headers(), body: stream } as Request, MAX_PROXY_BODY_BYTES);
    expect(streamed).toBeInstanceOf(Response);
    expect((streamed as Response).status).toBe(413);
    expect(calls).toHaveLength(0);

    const exact = new Uint8Array(MAX_PROXY_BODY_BYTES).fill(97);
    const exactBody = await readCappedBody(
      new Request("http://chat.local/api", { method: "PUT", body: exact }),
      MAX_PROXY_BODY_BYTES,
    );
    expect(exactBody).toBeInstanceOf(Uint8Array);
    expect((exactBody as Uint8Array).byteLength).toBe(MAX_PROXY_BODY_BYTES);

    let pulls = 0;
    const over = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        controller.enqueue(new Uint8Array(MAX_PROXY_BODY_BYTES + 1));
        controller.close();
      },
    });
    const overBody = await readCappedBody({ headers: new Headers(), body: over } as Request, MAX_PROXY_BODY_BYTES);
    expect(overBody).toBeInstanceOf(Response);
    expect((overBody as Response).status).toBe(413);
    expect(pulls).toBe(1);
    expect(MAX_PROXY_BODY_BYTES).toBe(1_048_576);
  });

  it("rejects an invalid UTF-8 body and does not forward it", async () => {
    const calls = stub();
    const response = await put(new Uint8Array([0x7b, 0xff, 0x7d]));
    expect(response.status).toBe(422);
    expect(calls).toHaveLength(0);
  });

  it("rejects invalid UTF-8 inside a JSON string", async () => {
    const calls = stub();
    const pre = new TextEncoder().encode('{"dsl":"');
    const post = new TextEncoder().encode(`","expected_version":1,"expected_ir_hash":"${H}"}`);
    const body = new Uint8Array(pre.length + 1 + post.length);
    body.set(pre, 0);
    body[pre.length] = 0xff;
    body.set(post, pre.length + 1);
    const response = await put(body);
    expect(response.status).toBe(422);
    expect(calls).toHaveLength(0);
  });

  it("returns 400 when the body stream errors", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([123]));
        controller.error(new Error("boom"));
      },
    });
    const response = await readCappedBody({ headers: new Headers(), body: stream } as Request, MAX_PROXY_BODY_BYTES);
    expect(response).toBeInstanceOf(Response);
    expect((response as Response).status).toBe(400);
  });

  it("returns 413 for an oversized content-length without draining the stream", async () => {
    let pulls = 0;
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        controller.enqueue(new Uint8Array(1024));
        if (pulls > 4) {
          controller.close();
        }
      },
    });
    const request = {
      headers: new Headers({ "content-length": String(MAX_PROXY_BODY_BYTES + 1) }),
      body: stream,
    } as Request;
    const response = await readCappedBody(request, MAX_PROXY_BODY_BYTES);
    expect(response).toBeInstanceOf(Response);
    expect((response as Response).status).toBe(413);
    expect(pulls).toBe(0);
  });

  it("does not mark a validate request as the web client", async () => {
    const { POST } = await import("@/app/api/strategies/[strategyId]/validate/route");
    const calls = stub();
    await POST(
      new Request(`http://chat.local/api/strategies/${STRATEGY_ID}/validate`, {
        method: "POST",
        body: '{"dsl":"x"}',
        headers: { "content-type": "application/json", "x-tradoor-client": "web" },
      }),
      { params: Promise.resolve({ strategyId: STRATEGY_ID }) },
    );
    const headers = new Headers(calls[0]?.init.headers);
    expect(headers.get("x-tradoor-client")).toBeNull();
  });
});
