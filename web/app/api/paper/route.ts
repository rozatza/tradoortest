import { forwardGet, paperEventsQuery } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const query = paperEventsQuery(request);
  if (query instanceof Response) {
    return query;
  }
  return forwardGet(`/paper${query}`);
}
