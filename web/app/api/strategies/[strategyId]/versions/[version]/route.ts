import { forwardGet, isAgentId, isVersionNumber, jsonError } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ strategyId: string; version: string }> },
): Promise<Response> {
  const { strategyId, version } = await context.params;
  if (!isAgentId(strategyId)) {
    return jsonError(422, "invalid_id", "strategy_id is invalid");
  }
  if (!isVersionNumber(version)) {
    return jsonError(422, "invalid_query", "version must be a whole number from 1 to 1000000");
  }
  return forwardGet(`/strategies/${strategyId}/versions/${version}`);
}
