import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Chat } from "@/components/chat";
import { formatSse } from "@/lib/sse";
import type { AgentEvent } from "@/lib/types";
import { agentEvent, PROPOSAL_ID, SESSION_ID, STRATEGY_ID } from "@/tests/fixtures";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function sseResponse(parts: string[], gate?: Promise<void>): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(encoder.encode(parts[0] ?? ""));
      if (gate) {
        await gate;
      }
      if (parts.length > 1) {
        controller.enqueue(encoder.encode(parts.slice(1).join("")));
      }
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function frames(events: AgentEvent[]): string {
  return events.map((event) => formatSse(event)).join("");
}

function install(handler: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function send(text: string) {
  fireEvent.change(screen.getByLabelText("Message"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("author chat", () => {
  it("streams tool steps in order and checks each one off", async () => {
    let release = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      const opening = frames([
        agentEvent(1, "user_message", { role: "user", content: "draft one" }),
        agentEvent(2, "tool_start", { tool: "validate", call_id: "c1", arguments: {} }),
        agentEvent(3, "tool_start", { tool: "write_strategy", call_id: "c2", arguments: {} }),
      ]);
      const closing = frames([
        agentEvent(4, "tool_complete", {
          tool: "validate",
          call_id: "c1",
          ok: true,
          output: { ok: true, data: { valid: true } },
        }),
        agentEvent(5, "tool_complete", {
          tool: "write_strategy",
          call_id: "c2",
          ok: true,
          output: {
            ok: true,
            data: { strategy_id: STRATEGY_ID, version: 1, name: "EMA cross", ir_hash: "ab" },
          },
        }),
        agentEvent(6, "assistant_message", { role: "assistant", content: "Drafted." }),
        agentEvent(7, "turn_complete", { status: "completed", turn_id: "d".repeat(32), error: null }),
      ]);
      return sseResponse([opening, closing], gate);
    });

    render(<Chat />);
    await send("draft one");
    expect(await screen.findByRole("status", { name: "validate working" })).toBeTruthy();
    expect(screen.getByRole("status", { name: "write_strategy working" }).textContent).toContain(
      "Working…",
    );
    expect(screen.queryByText("✓")).toBeNull();
    const working = screen.getAllByRole("status").map((item) => item.getAttribute("aria-label"));
    expect(working).toEqual(["validate working", "write_strategy working"]);

    release();
    expect(await screen.findByRole("status", { name: "write_strategy completed" })).toBeTruthy();
    const done = screen.getAllByRole("status");
    expect(done.map((item) => item.getAttribute("aria-label"))).toEqual([
      "validate completed",
      "write_strategy completed",
    ]);
    expect(done[0]?.textContent).toContain("✓");
    expect(done[1]?.textContent).toContain("✓");
    expect(screen.getByRole("region", { name: "Strategy written" }).textContent).toContain("EMA cross");
    expect(screen.getByRole("region", { name: "Strategy written" }).textContent).toContain(STRATEGY_ID);
    expect(screen.getByLabelText("Assistant").textContent).toContain("Drafted.");
    expect(setItem).not.toHaveBeenCalled();
  });

  it("sorts a reversed stream and shows a persisted worker error", async () => {
    install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      return sseResponse([
        frames([
          agentEvent(3, "turn_complete", {
            status: "error",
            turn_id: "d".repeat(32),
            error: {
              code: "internal_error",
              message: "the request could not be completed",
              path: null,
            },
          }),
          agentEvent(2, "error", {
            code: "internal_error",
            message: "the request could not be completed",
            path: null,
          }),
          agentEvent(1, "user_message", { role: "user", content: "boom" }),
        ]),
      ]);
    });
    render(<Chat />);
    await send("boom");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("internal_error: the request could not be completed");
    const articles = screen.getAllByRole("article");
    expect(articles[0]?.getAttribute("aria-label")).toBe("You");
    expect(articles[0]?.textContent).toContain("boom");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("shows an HTTP failure from the stream in the thread", async () => {
    install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      return jsonResponse(
        { error: { code: "service_unavailable", message: "the author API is unavailable", path: null } },
        503,
      );
    });
    render(<Chat />);
    await send("hello");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("service_unavailable: the author API is unavailable");
    expect(alert.textContent).toContain("hello");
  });

  it("accepts only from the button and shows a 409 conflict", async () => {
    const fetchMock = install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      if (url.includes("/messages/stream")) {
        return sseResponse([
          frames([
            agentEvent(1, "user_message", { role: "user", content: "edit" }),
            agentEvent(2, "proposal_created", {
              proposal_id: PROPOSAL_ID,
              strategy_id: STRATEGY_ID,
              from_version: 1,
              to_version: 2,
              status: "pending",
              dsl_diff: "--- a\n+++ b\n",
              ir_diff: [],
            }),
            agentEvent(3, "turn_complete", { status: "completed", turn_id: "d".repeat(32), error: null }),
          ]),
        ]);
      }
      return jsonResponse(
        { error: { code: "version_conflict", message: "strategy version conflict", path: "$.expected_version" } },
        409,
      );
    });
    render(<Chat />);
    await send("edit");
    const accept = await screen.findByRole("button", { name: "Accept" });
    await waitFor(() => {
      expect((accept as HTMLButtonElement).disabled).toBe(false);
    });
    const callsBefore = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(callsBefore.some((url) => url.includes("/accept") || url.includes("/reject"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("version_conflict: strategy version conflict");
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    });
    const decisionCalls = fetchMock.mock.calls.map((call) => String(call[0])).filter((url) => url.includes("/accept"));
    expect(decisionCalls).toEqual([`/api/agent/proposals/${PROPOSAL_ID}/accept`]);
  });

  it("shows an already-decided rejection and does not write a strategy card", async () => {
    install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      if (url.includes("/reject")) {
        return jsonResponse(
          {
            error: {
              code: "proposal_already_decided",
              message: "proposal is already decided",
              path: null,
            },
          },
          409,
        );
      }
      return sseResponse([
        frames([
          agentEvent(1, "user_message", { role: "user", content: "edit" }),
          agentEvent(2, "proposal_created", {
            proposal_id: PROPOSAL_ID,
            strategy_id: STRATEGY_ID,
            from_version: 1,
            to_version: 2,
            status: "pending",
            dsl_diff: "No diff text.",
            ir_diff: [],
          }),
          agentEvent(3, "turn_complete", { status: "completed", turn_id: "d".repeat(32), error: null }),
        ]),
      ]);
    });
    render(<Chat />);
    await send("edit");
    const reject = await screen.findByRole("button", { name: "Reject" });
    await waitFor(() => {
      expect((reject as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(reject);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("proposal_already_decided: proposal is already decided");
    expect(screen.queryByRole("region", { name: "Strategy updated" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Strategy written" })).toBeNull();
  });

  it("shows a strategy card after an explicit accept", async () => {
    install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      if (url.includes("/accept")) {
        return jsonResponse({
          proposal: { proposal_id: PROPOSAL_ID, status: "accepted", strategy_id: STRATEGY_ID },
          strategy: { strategy_id: STRATEGY_ID, version: 2, ir_hash: "bb" },
          event: agentEvent(4, "proposal_decided", {
            proposal_id: PROPOSAL_ID,
            decision: "accepted",
            status: "accepted",
            strategy_id: STRATEGY_ID,
            applied_version: 2,
            applied_ir_hash: "bb",
          }),
        });
      }
      return sseResponse([
        frames([
          agentEvent(1, "user_message", { role: "user", content: "edit" }),
          agentEvent(2, "proposal_created", {
            proposal_id: PROPOSAL_ID,
            strategy_id: STRATEGY_ID,
            from_version: 1,
            to_version: 2,
            status: "pending",
            dsl_diff: "--- a\n+++ b\n",
            ir_diff: [],
          }),
          agentEvent(3, "assistant_message", { role: "assistant", content: "Ready for review." }),
          agentEvent(3, "turn_complete", { status: "completed", turn_id: "d".repeat(32), error: null }),
        ]),
      ]);
    });
    render(<Chat />);
    await send("edit");
    const accept = await screen.findByRole("button", { name: "Accept" });
    await waitFor(() => {
      expect((accept as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(accept);
    const card = await screen.findByRole("region", { name: "Strategy updated" });
    expect(card.textContent).toContain(STRATEGY_ID);
    expect(card.textContent).toContain("2");
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    });
    expect(screen.getByText("accepted")).toBeTruthy();
  });

  it("shows an error and stops Working when the stream ends without turn_complete", async () => {
    install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      return sseResponse([
        frames([
          agentEvent(1, "user_message", { role: "user", content: "cut" }),
          agentEvent(2, "tool_start", { tool: "validate", call_id: "c1", arguments: {} }),
        ]),
      ]);
    });
    render(<Chat />);
    await send("cut");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("stream_incomplete: the response ended before the turn finished");
    expect(screen.queryByText("Working…")).toBeNull();
    expect(screen.getByRole("status", { name: "validate stopped" }).textContent).toContain("Stopped");
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
  });

  it("stops Working when the stream errors after a tool step", async () => {
    install((url) => {
      if (url === "/api/agent/sessions") {
        return jsonResponse({ session_id: SESSION_ID });
      }
      const encoder = new TextEncoder();
      let sent = false;
      const stream = new ReadableStream<Uint8Array>({
        pull(controller) {
          if (!sent) {
            sent = true;
            controller.enqueue(
              encoder.encode(
                frames([
                  agentEvent(1, "user_message", { role: "user", content: "cut" }),
                  agentEvent(2, "tool_start", { tool: "validate", call_id: "c1", arguments: {} }),
                ]),
              ),
            );
            return;
          }
          controller.error(new TypeError("network error"));
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    });
    render(<Chat />);
    await send("cut");
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("http_error");
    expect(alert.textContent).toContain("network error");
    expect(screen.queryByText("Working…")).toBeNull();
    expect(screen.getByRole("status", { name: "validate stopped" }).textContent).toContain("Stopped");
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
