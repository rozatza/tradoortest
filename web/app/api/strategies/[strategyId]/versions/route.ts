import { forwardGet, isAgentId, jsonError, pageQuery } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function GET(
  request: Request,
  context: { params: Promise<{ strategyId: string }> },
): Promise<Response> {
  const { strategyId } = await context.params;
  if (!isAgentId(strategyId)) {
    return jsonError(422, "invalid_id", "strategy_id is invalid");
  }
  const query = pageQuery(request);
  if (query instanceof Response) {
    return query;
  }
  return forwardGet(`/strategies/${strategyId}/versions${query}`);
}
