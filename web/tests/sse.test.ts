import { describe, expect, it } from "vitest";

import { eventFromFrame, formatSse, parseSseChunk, readEventStream } from "@/lib/sse";
import { agentEvent } from "@/tests/fixtures";

describe("parseSseChunk", () => {
  it("reassembles a frame split across chunks and keeps seq order", () => {
    const first = agentEvent(1, "user_message", { role: "user", content: "hello" });
    const second = agentEvent(2, "tool_start", { tool: "validate", call_id: "c1", arguments: {} });
    const wire = formatSse(second) + formatSse(first);
    const splitAt = 11;
    const head = parseSseChunk(wire.slice(0, splitAt));
    expect(head.frames).toEqual([]);
    const tail = parseSseChunk(head.rest + wire.slice(splitAt));
    expect(tail.rest).toBe("");
    const events = tail.frames.map((frame) => eventFromFrame(frame));
    expect(events.map((event) => event.seq)).toEqual([2, 1]);
    expect(events[1]?.payload.content).toBe("hello");
    expect(tail.frames[0]?.event).toBe("tool_start");
    expect(tail.frames[0]?.id).toBe("2");
  });

  it("reads a streamed body in arrival order", async () => {
    const user = agentEvent(1, "user_message", { role: "user", content: "hello" });
    const tool = agentEvent(2, "tool_start", { tool: "validate", call_id: "c1" });
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const wire = formatSse(user) + formatSse(tool);
        controller.enqueue(encoder.encode(wire.slice(0, 20)));
        controller.enqueue(encoder.encode(wire.slice(20)));
        controller.close();
      },
    });
    const seen: number[] = [];
    await readEventStream(new Response(stream), (event) => {
      seen.push(event.seq);
    });
    expect(seen).toEqual([1, 2]);
  });
});
