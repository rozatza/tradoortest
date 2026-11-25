import { forwardLive } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  return forwardLive("/live/flatten", request);
}
