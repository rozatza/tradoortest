import { forwardStream, isAgentId, jsonError } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ sessionId: string }> },
): Promise<Response> {
  const { sessionId } = await context.params;
  if (!isAgentId(sessionId)) {
    return jsonError(422, "invalid_id", "session_id is invalid");
  }
  return forwardStream(`/agent/sessions/${sessionId}/messages/stream`, request);
}
