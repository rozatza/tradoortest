import { forwardJson, isAgentId, jsonError } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ strategyId: string }> },
): Promise<Response> {
  const { strategyId } = await context.params;
  if (!isAgentId(strategyId)) {
    return jsonError(422, "invalid_id", "strategy_id is invalid");
  }
  return forwardJson(`/strategies/${strategyId}/validate`, request);
}
