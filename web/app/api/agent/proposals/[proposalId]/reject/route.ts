import { forwardJson, isAgentId, jsonError } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ proposalId: string }> },
): Promise<Response> {
  const { proposalId } = await context.params;
  if (!isAgentId(proposalId)) {
    return jsonError(422, "invalid_id", "proposal_id is invalid");
  }
  return forwardJson(`/agent/proposals/${proposalId}/reject`, request);
}
