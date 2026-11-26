const HEX_ID = /^[0-9a-f]{32}$/;

/**
 * Upper bound shared by the agent and strategy APIs
 * (`MAX_BODY_BYTES` / `_MAX_CONFIG_BODY`, 1 MiB). The proxy stops reading
 * once the next chunk would pass this, and does not buffer the rest.
 */
export const MAX_PROXY_BODY_BYTES = 1_048_576;

export function isAgentId(value: string): boolean {
  return HEX_ID.test(value);
}

export function isVersionNumber(value: string): boolean {
  if (!/^[1-9][0-9]{0,6}$/.test(value)) {
    return false;
  }
  const parsed = Number(value);
  return parsed >= 1 && parsed <= 1_000_000;
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

export function jsonError(
  status: number,
  code: string,
  message: string,
  path: string | null = null,
): Response {
  return Response.json(
    { error: { code, message, path } },
    { status, headers: { "cache-control": "no-store" } },
  );
}

/** Keep `limit` and `offset` only. Other query keys are dropped. */
export function pageQuery(request: Request): string | Response {
  const url = new URL(request.url);
  const params = new URLSearchParams();
  for (const name of ["limit", "offset"]) {
    const values = url.searchParams.getAll(name);
    if (values.length === 0) {
      continue;
    }
    if (values.length > 1) {
      return jsonError(422, "invalid_query", `${name} must be sent once`);
    }
    const value = values[0] ?? "";
    if (!/^[0-9]+$/.test(value) || (value.length > 1 && value.startsWith("0")) || value.length > 8) {
      return jsonError(422, "invalid_query", `${name} is invalid`);
    }
    params.set(name, value);
  }
  const text = params.toString();
  return text === "" ? "" : `?${text}`;
}

const PAPER_EVENT_QUERY = ["events_limit", "events_since"] as const;
const MAX_EVENT_CURSOR = BigInt("9007199254740991");

/**
 * Keep `events_limit` and `events_since` only. Other query keys are dropped.
 * The service applies the default limit when `events_limit` is omitted.
 */
export function paperEventsQuery(request: Request): string | Response {
  const url = new URL(request.url);
  const params = new URLSearchParams();
  for (const name of PAPER_EVENT_QUERY) {
    const values = url.searchParams.getAll(name);
    if (values.length === 0) {
      continue;
    }
    if (values.length > 1) {
      return jsonError(422, "invalid_query", `${name} must be sent once`);
    }
    const value = values[0] ?? "";
    if (!isEventWhole(value)) {
      return jsonError(422, "invalid_query", `${name} is invalid`);
    }
    if (name === "events_limit") {
      const parsed = Number(value);
      if (parsed < 1 || parsed > 200) {
        return jsonError(422, "invalid_query", "events_limit must be a whole number from 1 to 200");
      }
    } else if (BigInt(value) > MAX_EVENT_CURSOR) {
      return jsonError(422, "invalid_query", "events_since is invalid");
    }
    params.set(name, value);
  }
  const text = params.toString();
  return text === "" ? "" : `?${text}`;
}

function isEventWhole(value: string): boolean {
  if (!/^[0-9]+$/.test(value) || value.length > 16) {
    return false;
  }
  return value.length === 1 || !value.startsWith("0");
}

export async function forwardJson(
  path: string,
  request: Request,
  method: "POST" | "PUT" = "POST",
): Promise<Response> {
  return forward(path, request, "application/json", method);
}

export async function forwardStream(path: string, request: Request): Promise<Response> {
  return forward(path, request, "text/event-stream", "POST");
}

export async function forwardGet(path: string, accept = "application/json"): Promise<Response> {
  return sendUpstream(path, "GET", accept, null);
}

/** POST a fixed empty object. The paper deploy route ignores the body. */
export async function forwardEmptyPost(path: string): Promise<Response> {
  return sendUpstream(path, "POST", "application/json", "{}");
}

export async function forwardDelete(path: string): Promise<Response> {
  return sendUpstream(path, "DELETE", "application/json", null);
}

/**
 * Exact backtest reads the browser may trigger. Anything else, including
 * the agent tool surface and a query string, is not a path this proxy builds.
 */
export const BACKTEST_READS = [
  "/backtests/{job_id}",
  "/backtests/{job_id}/results",
  "/backtests/{job_id}/exports/trades.json",
  "/backtests/{job_id}/exports/trades.csv",
  "/backtests/{job_id}/exports/metrics.json",
  "/backtests/{job_id}/exports/metrics.csv",
  "/backtests/{job_id}/bars",
  "/backtests/{job_id}/indicators",
  "/backtests/{job_id}/analysis",
] as const;

export type BacktestRead = (typeof BACKTEST_READS)[number];

const BACKTEST_READ_SET: ReadonlySet<string> = new Set(BACKTEST_READS);

export function backtestPath(jobId: string, route: string): string | null {
  if (!isAgentId(jobId) || !BACKTEST_READ_SET.has(route)) {
    return null;
  }
  const suffix = route.slice("/backtests/{job_id}".length);
  if (suffix.includes("?") || suffix.includes("#") || suffix.includes("\\") || suffix.includes("//")) {
    return null;
  }
  return `/backtests/${jobId}${suffix}`;
}

/** GET one allowlisted backtest path. The job id uses the same 32-hex check as other ids. */
export async function backtestGet(
  context: { params: Promise<{ jobId: string }> },
  route: BacktestRead,
  accept = "application/json",
): Promise<Response> {
  const { jobId } = await context.params;
  const path = backtestPath(jobId, route);
  if (path === null) {
    return jsonError(422, "invalid_id", "job_id is invalid");
  }
  return forwardGet(path, accept);
}

const IR_HASH = /^[0-9a-f]{64}$/;
const MAX_STRATEGY_VERSION = 1_000_000;

/**
 * Refuse a strategy save that does not carry a usable concurrency pair.
 * Returns null when the original text may be forwarded unchanged.
 */
export function strategySaveError(text: string): Response | null {
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    return jsonError(422, "invalid_json", "request body is not JSON");
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return jsonError(422, "invalid_json", "request body must be a JSON object");
  }
  const record = payload as Record<string, unknown>;
  const versionSet = Object.hasOwn(record, "expected_version") && record.expected_version !== null;
  const hashSet = Object.hasOwn(record, "expected_ir_hash") && record.expected_ir_hash !== null;
  if (!versionSet || !hashSet) {
    return jsonError(
      422,
      "invalid_request",
      "expected_version and expected_ir_hash must be sent together",
      "$",
    );
  }
  const version = record.expected_version;
  if (
    typeof version !== "number" ||
    !Number.isInteger(version) ||
    version < 1 ||
    version > MAX_STRATEGY_VERSION
  ) {
    return jsonError(
      422,
      "invalid_request",
      "expected_version must be a whole number from 1 to 1000000",
      "$.expected_version",
    );
  }
  const digest = record.expected_ir_hash;
  if (typeof digest !== "string" || !IR_HASH.test(digest)) {
    return jsonError(
      422,
      "invalid_request",
      "expected_ir_hash must be 64 lowercase hex characters",
      "$.expected_ir_hash",
    );
  }
  return null;
}

/** Read, require the concurrency pair, then PUT the original bytes. */
export async function forwardStrategySave(path: string, request: Request): Promise<Response> {
  const capped = await readCappedBody(request, MAX_PROXY_BODY_BYTES);
  if (capped instanceof Response) {
    return capped;
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(capped);
  } catch {
    return jsonError(422, "invalid_json", "request body is not JSON");
  }
  const rejected = strategySaveError(text);
  if (rejected) {
    return rejected;
  }
  return sendUpstream(path, "PUT", "application/json", text, { "x-tradoor-client": "web" });
}

/**
 * Read the request body up to `limit` bytes. A chunk that would pass the
 * cap is not appended, and the stream is cancelled so later chunks are not
 * pulled.
 */
export async function readCappedBody(request: Request, limit: number): Promise<Uint8Array | Response> {
  const declared = request.headers.get("content-length");
  if (declared !== null) {
    if (!/^[0-9]+$/.test(declared) || (declared.length > 1 && declared.startsWith("0"))) {
      await request.body?.cancel();
      return jsonError(400, "invalid_request", "content length is invalid");
    }
    if (declared.length > 8 || Number(declared) > limit) {
      await request.body?.cancel();
      return jsonError(413, "payload_too_large", "request body is too large");
    }
  }
  const stream = request.body;
  if (stream === null) {
    return new Uint8Array();
  }
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      if (!value || value.byteLength === 0) {
        continue;
      }
      if (value.byteLength > limit - total) {
        await reader.cancel();
        return jsonError(413, "payload_too_large", "request body is too large");
      }
      chunks.push(value);
      total += value.byteLength;
    }
  } catch {
    await reader.cancel().catch(() => undefined);
    return jsonError(400, "invalid_request", "request body could not be read");
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

async function forward(
  path: string,
  request: Request,
  accept: string,
  method: "POST" | "PUT",
): Promise<Response> {
  const capped = await readCappedBody(request, MAX_PROXY_BODY_BYTES);
  if (capped instanceof Response) {
    return capped;
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(capped);
  } catch {
    return jsonError(422, "invalid_json", "request body is not JSON");
  }
  return sendUpstream(path, method, accept, text);
}

const LIVE_PATHS = new Set(["/live/prepare", "/live/confirm", "/live/flatten"]);
const MAX_LIVE_BODY_BYTES = 8_192;
const LIVE_ORIGIN_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

/**
 * Browser Origin for a live POST. Loopback only. A missing or cross-site
 * Origin is refused before the control token is read onto a request.
 */
export function liveOriginAllowed(value: string | null): boolean {
  if (value === null || value === "") {
    return false;
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return false;
  }
  if (url.username !== "" || url.password !== "" || url.search !== "" || url.hash !== "") {
    return false;
  }
  if (url.pathname !== "/" && url.pathname !== "") {
    return false;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return false;
  }
  const host = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  return LIVE_ORIGIN_HOSTS.has(host);
}

/**
 * Forward one live control call. The browser does not supply the token.
 * A cross-site Origin is rejected before the token is attached. An
 * allowlisted Origin is replaced with the loopback origin the service
 * expects, and the token is not copied into the response.
 */
export async function forwardLive(path: string, request: Request): Promise<Response> {
  if (!LIVE_PATHS.has(path)) {
    return jsonError(404, "not_found", "that live route is not proxied");
  }
  if (!liveOriginAllowed(request.headers.get("origin"))) {
    return jsonError(403, "control_forbidden", "cross-site live control is refused");
  }
  const token = process.env.LIVE_CONTROL_TOKEN;
  if (typeof token !== "string" || token === "" || /[\u0000-\u001f\u007f]/.test(token)) {
    return jsonError(503, "service_unavailable", "live control is not configured");
  }
  const capped = await readCappedBody(request, MAX_LIVE_BODY_BYTES);
  if (capped instanceof Response) {
    return capped;
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(capped);
    const payload: unknown = JSON.parse(text);
    if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
      return jsonError(422, "invalid_json", "request body must be a JSON object");
    }
  } catch {
    return jsonError(422, "invalid_json", "request body is not JSON");
  }
  return sendUpstream(path, "POST", "application/json", text, {
    origin: "http://127.0.0.1",
    "x-live-token": token,
  });
}

async function sendUpstream(
  path: string,
  method: string,
  accept: string,
  body: string | null,
  extraHeaders?: Record<string, string>,
): Promise<Response> {
  const origin = authorOrigin();
  if (origin === null) {
    return jsonError(503, "service_unavailable", "the author API is not configured");
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${origin}${path}`, {
      method,
      headers: {
        accept,
        ...(body === null ? {} : { "content-type": "application/json" }),
        ...extraHeaders,
      },
      body: body === null ? undefined : body,
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
