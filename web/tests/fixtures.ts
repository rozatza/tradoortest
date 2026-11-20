import type { AgentEvent } from "@/lib/types";

export const SESSION_ID = "a".repeat(32);
export const PROPOSAL_ID = "b".repeat(32);
export const STRATEGY_ID = "c".repeat(32);
export const TURN_ID = "d".repeat(32);

export function agentEvent(
  seq: number,
  type: string,
  payload: Record<string, unknown>,
): AgentEvent {
  return {
    seq,
    event_id: `event-${seq}-${type}`,
    session_id: SESSION_ID,
    turn_id: TURN_ID,
    type,
    created_at: "2026-01-01T00:00:00Z",
    payload,
  };
}
