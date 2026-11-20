import { forwardGet, pageQuery } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const query = pageQuery(request);
  if (query instanceof Response) {
    return query;
  }
  return forwardGet(`/strategies${query}`);
}
