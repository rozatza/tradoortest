import { forwardDelete, forwardEmptyPost, isAgentId, isVersionNumber, jsonError } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function POST(
  request: Request,
  context: { params: Promise<{ strategyId: string; version: string }> },
): Promise<Response> {
  const path = await paperPath(context.params);
  if (path instanceof Response) {
    return path;
  }
  await request.body?.cancel().catch(() => undefined);
  return forwardEmptyPost(path);
}

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ strategyId: string; version: string }> },
): Promise<Response> {
  const path = await paperPath(context.params);
  if (path instanceof Response) {
    return path;
  }
  return forwardDelete(path);
}

async function paperPath(
  params: Promise<{ strategyId: string; version: string }>,
): Promise<string | Response> {
  const { strategyId, version } = await params;
  if (!isAgentId(strategyId)) {
    return jsonError(422, "invalid_id", "strategy_id is invalid");
  }
  if (!isVersionNumber(version)) {
    return jsonError(422, "invalid_query", "version must be a whole number from 1 to 1000000");
  }
  return `/strategies/${strategyId}/versions/${version}/paper`;
}
