import type { AgentEvent } from "@/lib/types";

export type SseFrame = {
  id?: string;
  event?: string;
  data: string;
};

/** Split a text/event-stream buffer on blank lines. `rest` is the incomplete tail. */
export function parseSseChunk(buffer: string): { frames: SseFrame[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const parts = normalized.split("\n\n");
  const rest = parts.pop() ?? "";
  const frames: SseFrame[] = [];
  for (const part of parts) {
    if (part.trim() === "") {
      continue;
    }
    let id: string | undefined;
    let event: string | undefined;
    const dataLines: string[] = [];
    for (const line of part.split("\n")) {
      if (line.startsWith(":")) {
        continue;
      }
      if (line.startsWith("id:")) {
        id = line.slice(3).replace(/^ /, "");
      } else if (line.startsWith("event:")) {
        event = line.slice(6).replace(/^ /, "");
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).replace(/^ /, ""));
      }
    }
    if (dataLines.length > 0) {
      frames.push({ id, event, data: dataLines.join("\n") });
    }
  }
  return { frames, rest };
}

export function formatSse(event: AgentEvent): string {
  return `id: ${event.seq}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

export function eventFromFrame(frame: SseFrame): AgentEvent {
  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch {
    throw new Error("event is not JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("event is not an object");
  }
  const record = parsed as Record<string, unknown>;
  const seq = record.seq;
  const type = record.type;
  if (typeof seq !== "number" || !Number.isInteger(seq) || typeof type !== "string") {
    throw new Error("event is missing seq or type");
  }
  const payload = record.payload;
  return {
    seq,
    event_id: typeof record.event_id === "string" ? record.event_id : type,
    session_id: typeof record.session_id === "string" ? record.session_id : "",
    turn_id: typeof record.turn_id === "string" ? record.turn_id : null,
    type,
    created_at: typeof record.created_at === "string" ? record.created_at : "",
    payload: payload && typeof payload === "object" && !Array.isArray(payload)
      ? (payload as Record<string, unknown>)
      : {},
  };
}

export async function readEventStream(
  response: Response,
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  const body = response.body;
  if (!body) {
    throw new Error("empty stream");
  }
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const emit = (frames: SseFrame[]) => {
    for (const frame of frames) {
      onEvent(eventFromFrame(frame));
    }
  };
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSseChunk(buffer);
    buffer = parsed.rest;
    emit(parsed.frames);
  }
  buffer += decoder.decode();
  if (buffer.trim() !== "") {
    const parsed = parseSseChunk(`${buffer}\n\n`);
    emit(parsed.frames);
  }
}
