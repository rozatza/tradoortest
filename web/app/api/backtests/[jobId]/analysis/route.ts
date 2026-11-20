import { backtestGet } from "@/lib/upstream";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ jobId: string }> },
): Promise<Response> {
  return backtestGet(context, "/backtests/{job_id}/analysis");
}
