import { readEventStream } from "@/lib/sse";
import type { AgentEvent, StrategyFields } from "@/lib/types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export type DecisionResult =
  | {
      ok: true;
      event: AgentEvent | null;
      strategy: StrategyFields | null;
      proposalStatus: string | null;
    }
  | { ok: false; status: number; code: string; message: string };

export async function createSession(): Promise<{ session_id: string }> {
  const response = await fetch("/api/agent/sessions", {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: "{}",
  });
  if (!response.ok) {
    throw await readApiError(response);
  }
  const body = (await response.json()) as { session_id?: unknown };
  if (typeof body.session_id !== "string" || body.session_id === "") {
    throw new ApiError(502, "invalid_response", "the author API returned an invalid session");
  }
  return { session_id: body.session_id };
}

export async function streamMessage(
  sessionId: string,
  content: string,
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  const response = await fetch(`/api/agent/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({ content }),
  });
  if (!response.ok) {
    throw await readApiError(response);
  }
  const type = response.headers.get("content-type") ?? "";
  if (!type.includes("text/event-stream")) {
    throw await readApiError(response);
  }
  await readEventStream(response, onEvent);
}

export async function decideProposal(
  proposalId: string,
  decision: "accept" | "reject",
): Promise<DecisionResult> {
  const response = await fetch(`/api/agent/proposals/${proposalId}/${decision}`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: "{}",
  });
  if (!response.ok) {
    const error = await readApiError(response);
    return { ok: false, status: error.status, code: error.code, message: error.message };
  }
  const body = (await response.json()) as Record<string, unknown>;
  return {
    ok: true,
    event: isEvent(body.event) ? body.event : null,
    strategy: strategyFields(body.strategy),
    proposalStatus: proposalStatus(body.proposal),
  };
}

async function readApiError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: { code?: unknown; message?: unknown } };
    const code = body.error?.code;
    const message = body.error?.message;
    if (typeof message === "string" && message !== "") {
      return new ApiError(
        response.status,
        typeof code === "string" && code !== "" ? code : "http_error",
        message,
      );
    }
  } catch {
    // The body was not the JSON error envelope.
  }
  return new ApiError(response.status, "http_error", `request failed (${response.status})`);
}

function isEvent(value: unknown): value is AgentEvent {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return typeof record.seq === "number" && typeof record.type === "string" && !!record.payload;
}

function strategyFields(value: unknown): StrategyFields | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.strategy_id !== "string" || record.strategy_id === "") {
    return null;
  }
  return {
    strategyId: record.strategy_id,
    version: typeof record.version === "number" ? record.version : null,
    irHash: typeof record.ir_hash === "string" ? record.ir_hash : null,
    name: typeof record.name === "string" ? record.name : null,
  };
}

function proposalStatus(value: unknown): string | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const status = (value as Record<string, unknown>).status;
  return typeof status === "string" ? status : null;
}
