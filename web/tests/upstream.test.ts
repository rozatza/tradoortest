import { afterEach, describe, expect, it, vi } from "vitest";

import { POST as acceptProposal } from "@/app/api/agent/proposals/[proposalId]/accept/route";
import { POST as streamMessage } from "@/app/api/agent/sessions/[sessionId]/messages/stream/route";
import { POST as createSession } from "@/app/api/agent/sessions/route";
import { authorOrigin } from "@/lib/upstream";
import { SESSION_ID } from "@/tests/fixtures";

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
});
