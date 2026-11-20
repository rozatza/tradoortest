import { forwardGet, forwardStrategySave, isAgentId, jsonError } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ strategyId: string }> },
): Promise<Response> {
  const { strategyId } = await context.params;
  if (!isAgentId(strategyId)) {
    return jsonError(422, "invalid_id", "strategy_id is invalid");
  }
  return forwardGet(`/strategies/${strategyId}`);
}

export async function PUT(
  request: Request,
  context: { params: Promise<{ strategyId: string }> },
): Promise<Response> {
  const { strategyId } = await context.params;
  if (!isAgentId(strategyId)) {
    return jsonError(422, "invalid_id", "strategy_id is invalid");
  }
  return forwardStrategySave(`/strategies/${strategyId}`, request);
}
