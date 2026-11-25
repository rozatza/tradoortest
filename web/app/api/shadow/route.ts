import { forwardGet } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  return forwardGet("/shadow");
}
