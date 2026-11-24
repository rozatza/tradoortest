import { afterEach, describe, expect, it, vi } from "vitest";

import { GET as getPaper } from "@/app/api/paper/route";
import { DELETE, POST } from "@/app/api/strategies/[strategyId]/versions/[version]/paper/route";

const ID = "a".repeat(32);

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.AGENT_API_ORIGIN;
});

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

describe("paper proxy", () => {
  it("reads the paper book and does not forward a caller credential", async () => {
    const calls = stub();
    const response = await getPaper(new Request("http://chat.local/api/paper"));
    expect(response.status).toBe(200);
    expect(calls[0]?.url).toBe("http://127.0.0.1:8000/paper");
    expect(calls[0]?.init.method).toBe("GET");
    const headers = new Headers(calls[0]?.init.headers);
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("cookie")).toBeNull();
  });

  it("deploys and undeploys one version, and rejects a bad id before the upstream call", async () => {
    const calls = stub();
    const context = { params: Promise.resolve({ strategyId: ID, version: "3" }) };
    const posted = await POST(
      new Request(`http://chat.local/api/strategies/${ID}/versions/3/paper`, {
        method: "POST",
        body: '{"wallet":"no"}',
        headers: { authorization: "Bearer z", cookie: "a=b" },
      }),
      context,
    );
    expect(posted.status).toBe(200);
    expect(calls[0]?.url).toBe(`http://127.0.0.1:8000/strategies/${ID}/versions/3/paper`);
    expect(calls[0]?.init.method).toBe("POST");
    expect(calls[0]?.init.body).toBe("{}");
    const headers = new Headers(calls[0]?.init.headers);
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("cookie")).toBeNull();
    const removed = await DELETE(new Request("http://chat.local/paper", { method: "DELETE" }), context);
    expect(removed.status).toBe(200);
    expect(calls[1]?.init.method).toBe("DELETE");
    expect(calls[1]?.url).toBe(calls[0]?.url);
    const rejected = await POST(
      new Request("http://chat.local/api/strategies/nope/versions/3/paper", { method: "POST", body: "{}" }),
      { params: Promise.resolve({ strategyId: "nope", version: "3" }) },
    );
    expect(rejected.status).toBe(422);
    expect(calls).toHaveLength(2);
  });

  it("forwards the event page and drops every other query key", async () => {
    const calls = stub();
    const response = await getPaper(
      new Request("http://chat.local/api/paper?events_limit=10&events_since=4&wallet=no"),
    );
    expect(response.status).toBe(200);
    expect(calls[0]?.url).toBe("http://127.0.0.1:8000/paper?events_limit=10&events_since=4");
    const rejected = await getPaper(new Request("http://chat.local/api/paper?events_limit=0"));
    expect(rejected.status).toBe(422);
    expect(calls).toHaveLength(1);
    const duplicated = await getPaper(
      new Request("http://chat.local/api/paper?events_since=1&events_since=2"),
    );
    expect(duplicated.status).toBe(422);
    expect(calls).toHaveLength(1);
  });
});
