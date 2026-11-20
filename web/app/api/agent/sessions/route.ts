import { forwardJson } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export function POST(request: Request): Promise<Response> {
  return forwardJson("/agent/sessions", request);
}
