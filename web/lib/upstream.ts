const HEX_ID = /^[0-9a-f]{32}$/;

export function isAgentId(value: string): boolean {
  return HEX_ID.test(value);
}

/** Server-side origin for the author API. Never read this from a client component. */
export function authorOrigin(): string | null {
  const raw = process.env.AGENT_API_ORIGIN;
  if (typeof raw !== "string") {
    return null;
  }
  const trimmed = raw.trim();
  if (trimmed === "") {
    return null;
  }
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    return null;
  }
  if (url.username !== "" || url.password !== "" || url.search !== "" || url.hash !== "") {
    return null;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return null;
  }
  if (url.pathname !== "/" && url.pathname !== "") {
    return null;
  }
  return url.origin;
}

export function jsonError(status: number, code: string, message: string): Response {
  return Response.json(
    { error: { code, message, path: null } },
    { status, headers: { "cache-control": "no-store" } },
  );
}

export async function forwardJson(path: string, request: Request): Promise<Response> {
  return forward(path, request, "application/json");
}

export async function forwardStream(path: string, request: Request): Promise<Response> {
  return forward(path, request, "text/event-stream");
}

async function forward(path: string, request: Request, accept: string): Promise<Response> {
  const origin = authorOrigin();
  if (origin === null) {
    return jsonError(503, "service_unavailable", "the author API is not configured");
  }
  const body = await request.text();
  let upstream: Response;
  try {
    upstream = await fetch(`${origin}${path}`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept,
      },
      body,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return jsonError(503, "service_unavailable", "the author API is unavailable");
  }
  if (upstream.status >= 300 && upstream.status < 400) {
    return jsonError(503, "service_unavailable", "the author API is unavailable");
  }
  if (accept === "text/event-stream") {
    const type = upstream.headers.get("content-type") ?? "";
    if (upstream.ok && type.includes("text/event-stream") && upstream.body) {
      return new Response(upstream.body, {
        status: 200,
        headers: {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
          "x-accel-buffering": "no",
        },
      });
    }
  }
  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/json",
      "cache-control": "no-store",
    },
  });
}
