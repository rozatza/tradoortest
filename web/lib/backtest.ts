/**
 * Read a backtest job through the Next proxy and shape it for display.
 *
 * Numbers shown in the panel are the strings and counts on the result.
 * This module does not derive a ratio, a drawdown, or a fill.
 */

export const JOB_ID = /^[0-9a-f]{32}$/;
export const EQUITY_DRAW_CAP = 500;
export const TRADE_PAGE_SIZE = 50;
export const BACKTEST_TIMEOUT_MS = 20_000;
export const UNAVAILABLE = "unavailable";

export const METRIC_FIELDS = [
  "net_return",
  "annualized_return",
  "max_drawdown",
  "sharpe",
  "sortino",
  "calmar",
  "win_rate",
  "payoff_ratio",
] as const;

export const METRIC_FACTS = [
  "initial_equity",
  "final_equity",
  "bar_count",
  "bars_per_year",
  "grid_bars",
  "missing_bars",
  "synthetic_bars",
  "long_no_trade_bars",
  "long_no_trade_minutes",
  "long_no_trade_threshold",
  "suspect_outage",
  "suspect_outage_bars",
  "suspect_outage_minutes",
  "missing_bars_in_position",
  "synthetic_bars_in_position",
  "long_no_trade_bars_in_position",
  "long_no_trade_minutes_in_position",
  "suspect_outage_bars_in_position",
  "suspect_outage_minutes_in_position",
  "take_profit_limit_below_low",
] as const;

/** Same columns as `TRADE_COLUMNS` in the trade log. */
export const TRADE_COLUMNS = [
  "trade_id",
  "asset_id",
  "status",
  "entry_time",
  "exit_time",
  "qty",
  "entry_price",
  "exit_price",
  "entry_notional",
  "exit_notional",
  "commission",
  "realized_pnl",
  "unrealized_pnl",
  "limit_below_low",
  "overlaps_suspect_outage",
  "synthetic_bars",
  "missing_bars",
  "long_no_trade_bars",
  "long_no_trade_minutes",
  "suspect_outage_bars",
  "suspect_outage_minutes",
] as const;

export const DOWNLOADS = [
  { kind: "trades.csv", label: "Download trades.csv", accept: "text/csv" },
  { kind: "trades.json", label: "Download trades.json", accept: "application/json" },
  { kind: "metrics.csv", label: "Download metrics.csv", accept: "text/csv" },
  { kind: "metrics.json", label: "Download metrics.json", accept: "application/json" },
  { kind: "result.json", label: "Download result JSON", accept: "application/json" },
] as const;

export type DownloadKind = (typeof DOWNLOADS)[number]["kind"];

export type EquityPoint = {
  open_time: number;
  equity: string | number;
  cash: string | number;
};

export type RawText = {
  shown: string;
  title?: string;
};

export type LoadedBacktest =
  | { kind: "pending"; jobId: string; status: string }
  | { kind: "ready"; jobId: string; result: Record<string, unknown> };

export class BacktestRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "BacktestRequestError";
    this.status = status;
    this.code = code;
  }
}

const JOB_STATUSES = new Set(["queued", "running", "succeeded", "failed"]);

export function isJobId(value: string): boolean {
  return JOB_ID.test(value);
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/** Show the API value. Null and missing are `unavailable`, and the title keeps the raw text. */
export function rawText(value: unknown): RawText {
  if (typeof value === "string") {
    if (value === "") {
      return { shown: UNAVAILABLE };
    }
    return { shown: value, title: value };
  }
  if (typeof value === "boolean") {
    const shown = value ? "true" : "false";
    return { shown, title: shown };
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    const shown = String(value);
    return { shown, title: shown };
  }
  return { shown: UNAVAILABLE };
}

/**
 * Decimal text the job runner stores with ``format(Decimal, "f")``.
 * Optional minus, no exponent, and no leading zeros on the integer part.
 */
const DECIMAL_MARK = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/;

/** A finite number, or the exact decimal text the backend stores. */
export function isStoredMark(value: unknown): value is string | number {
  if (typeof value === "number") {
    return Number.isFinite(value);
  }
  return typeof value === "string" && DECIMAL_MARK.test(value);
}

/** The mark's own text. A string is unchanged. A number uses `String`, and `-0` stays "-0". */
export function markText(value: string | number): string {
  if (typeof value === "string") {
    return value;
  }
  if (Object.is(value, -0)) {
    return "-0";
  }
  return String(value);
}

export function isEquityPoint(value: unknown): value is EquityPoint {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.open_time === "number" &&
    Number.isFinite(value.open_time) &&
    isStoredMark(value.equity) &&
    isStoredMark(value.cash)
  );
}

/**
 * Points to draw, chosen from the series the API returned.
 * First and last stay. Interior buckets keep the lower and higher equity
 * already on a point. No new equity string is created.
 */
export function pointsForCurve(points: readonly EquityPoint[], cap: number): EquityPoint[] {
  if (cap < 1) {
    return [];
  }
  if (points.length <= cap) {
    return points.slice();
  }
  if (cap === 1) {
    return [points[0]!];
  }
  const chosen: EquityPoint[] = [points[0]!];
  const slots = cap - 2;
  const buckets = Math.max(1, Math.floor(slots / 2));
  const interior = points.length - 2;
  for (let bucket = 0; bucket < buckets; bucket += 1) {
    const start = 1 + Math.floor((bucket * interior) / buckets);
    const end = 1 + Math.floor(((bucket + 1) * interior) / buckets);
    let low = start;
    let high = start;
    for (let index = start + 1; index < end; index += 1) {
      const equity = points[index]!.equity;
      if (compareMark(equity, points[low]!.equity) < 0) {
        low = index;
      }
      if (compareMark(equity, points[high]!.equity) > 0) {
        high = index;
      }
    }
    const firstIndex = Math.min(low, high);
    const secondIndex = Math.max(low, high);
    if (chosen.length < cap - 1) {
      chosen.push(points[firstIndex]!);
    }
    if (secondIndex !== firstIndex && chosen.length < cap - 1) {
      chosen.push(points[secondIndex]!);
    }
  }
  chosen.push(points[points.length - 1]!);
  return chosen;
}

/** SVG coordinates for points that already passed the draw cap. Null when a mark is not finite. */
export function curveCoordinates(points: readonly EquityPoint[]): string | null {
  if (points.length === 0) {
    return null;
  }
  const ys: number[] = [];
  for (const point of points) {
    if (!isStoredMark(point.equity)) {
      return null;
    }
    const value = Number(point.equity);
    if (!Number.isFinite(value)) {
      return null;
    }
    ys.push(value);
  }
  let min = ys[0]!;
  let max = ys[0]!;
  for (const value of ys) {
    if (value < min) {
      min = value;
    }
    if (value > max) {
      max = value;
    }
  }
  const span = max - min;
  const width = 100;
  const height = 40;
  const pad = 1;
  const coords: string[] = [];
  for (let index = 0; index < points.length; index += 1) {
    const x =
      points.length === 1 ? width / 2 : pad + (index * (width - pad * 2)) / (points.length - 1);
    const y = span === 0 ? height / 2 : pad + (1 - (ys[index]! - min) / span) * (height - pad * 2);
    coords.push(`${x},${y}`);
  }
  return coords.join(" ");
}

export function tradePage(trades: readonly unknown[], page: number): unknown[] {
  const size = TRADE_PAGE_SIZE;
  const start = Math.max(0, page) * size;
  return trades.slice(start, start + size);
}

export async function loadBacktest(jobId: string): Promise<LoadedBacktest> {
  if (!isJobId(jobId)) {
    throw new BacktestRequestError(422, "invalid_id", "job_id is invalid");
  }
  const statusResponse = await request(`/api/backtests/${jobId}`, "application/json");
  if (!statusResponse.ok) {
    throw await errorFrom(statusResponse);
  }
  const status = readStatus(await readJson(statusResponse));
  if (!status) {
    throw new BacktestRequestError(200, "invalid_response", "the response was not a backtest job");
  }
  if (status.status === "failed") {
    throw new BacktestRequestError(
      422,
      status.errorCode ?? "backtest_error",
      status.errorMessage ?? "the backtest could not finish",
    );
  }
  if (status.status === "queued" || status.status === "running") {
    return { kind: "pending", jobId, status: status.status };
  }
  const resultsResponse = await request(`/api/backtests/${jobId}/results`, "application/json");
  if (!resultsResponse.ok) {
    throw await errorFrom(resultsResponse);
  }
  const body = await readJson(resultsResponse);
  if (!isRecord(body)) {
    throw new BacktestRequestError(200, "invalid_response", "the response was not a backtest result");
  }
  return { kind: "ready", jobId, result: body };
}

const CHART_READS = new Set(["/bars", "/indicators", "/analysis"]);

/** GET one allowlisted chart read. The path is not taken from the caller. */
export async function fetchJobJson(jobId: string, suffix: string): Promise<unknown> {
  if (!isJobId(jobId) || !CHART_READS.has(suffix)) {
    throw new BacktestRequestError(422, "invalid_id", "job_id is invalid");
  }
  const response = await request(`/api/backtests/${jobId}${suffix}`, "application/json");
  if (!response.ok) {
    throw await errorFrom(response);
  }
  return readJson(response);
}

export async function downloadBacktest(jobId: string, kind: DownloadKind): Promise<void> {
  if (!isJobId(jobId)) {
    throw new BacktestRequestError(422, "invalid_id", "job_id is invalid");
  }
  const spec = DOWNLOADS.find((item) => item.kind === kind);
  if (!spec) {
    throw new BacktestRequestError(404, "not_found", "export was not found");
  }
  const url =
    kind === "result.json"
      ? `/api/backtests/${jobId}/results`
      : `/api/backtests/${jobId}/exports/${kind}`;
  const response = await request(url, spec.accept);
  if (!response.ok) {
    throw await errorFrom(response);
  }
  const bytes = await response.arrayBuffer();
  saveBytes(bytes, `backtest-${jobId}-${kind}`);
}

async function request(url: string, accept: string): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), BACKTEST_TIMEOUT_MS);
  try {
    return await fetch(url, {
      method: "GET",
      headers: { accept },
      cache: "no-store",
      signal: controller.signal,
    });
  } catch {
    if (controller.signal.aborted) {
      throw new BacktestRequestError(0, "timeout", "the request timed out");
    }
    throw new BacktestRequestError(0, "network_error", "the request could not be completed");
  } finally {
    clearTimeout(timer);
  }
}

async function readJson(response: Response): Promise<unknown> {
  let text: string;
  try {
    text = await response.text();
  } catch {
    throw new BacktestRequestError(response.status, "network_error", "the request could not be completed");
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new BacktestRequestError(response.status, "invalid_response", "the response was not JSON");
  }
}

async function errorFrom(response: Response): Promise<BacktestRequestError> {
  let text: string;
  try {
    text = await response.text();
  } catch {
    return new BacktestRequestError(response.status, "network_error", "the request could not be completed");
  }
  try {
    const body = JSON.parse(text) as { error?: { code?: unknown; message?: unknown } };
    const message = body.error?.message;
    const code = body.error?.code;
    if (typeof message === "string" && message !== "") {
      return new BacktestRequestError(
        response.status,
        typeof code === "string" && code !== "" ? code : "http_error",
        message,
      );
    }
  } catch {
    return new BacktestRequestError(response.status, "invalid_response", "the response was not JSON");
  }
  return new BacktestRequestError(response.status, "http_error", `request failed (${response.status})`);
}

function readStatus(body: unknown): {
  status: string;
  errorCode: string | null;
  errorMessage: string | null;
} | null {
  if (!isRecord(body) || typeof body.status !== "string" || !JOB_STATUSES.has(body.status)) {
    return null;
  }
  const error = isRecord(body.error) ? body.error : null;
  return {
    status: body.status,
    errorCode: error && typeof error.code === "string" && error.code !== "" ? error.code : null,
    errorMessage:
      error && typeof error.message === "string" && error.message !== "" ? error.message : null,
  };
}

function saveBytes(bytes: ArrayBuffer, filename: string): void {
  const blob = new Blob([bytes]);
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function compareMark(left: string | number, right: string | number): number {
  if (typeof left === "string" && typeof right === "string") {
    return compareDecimal(left, right);
  }
  const leftNumber = typeof left === "number" ? left : Number(left);
  const rightNumber = typeof right === "number" ? right : Number(right);
  if (Number.isFinite(leftNumber) && Number.isFinite(rightNumber)) {
    if (leftNumber === rightNumber) {
      return 0;
    }
    return leftNumber < rightNumber ? -1 : 1;
  }
  return compareDecimal(markText(left), markText(right));
}

function compareDecimal(left: string, right: string): number {
  const a = decimalParts(left);
  const b = decimalParts(right);
  if (!a || !b) {
    if (left === right) {
      return 0;
    }
    return left < right ? -1 : 1;
  }
  if (a.negative !== b.negative) {
    return a.negative ? -1 : 1;
  }
  const magnitude = compareParts(a, b);
  return a.negative ? -magnitude : magnitude;
}

function decimalParts(value: string): { negative: boolean; whole: string; frac: string } | null {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) {
    return null;
  }
  return {
    negative: match[1] === "-",
    whole: (match[2] ?? "0").replace(/^0+(?=\d)/, ""),
    frac: (match[3] ?? "").replace(/0+$/, ""),
  };
}

function compareParts(
  left: { whole: string; frac: string },
  right: { whole: string; frac: string },
): number {
  if (left.whole.length !== right.whole.length) {
    return left.whole.length < right.whole.length ? -1 : 1;
  }
  if (left.whole !== right.whole) {
    return left.whole < right.whole ? -1 : 1;
  }
  const frac = left.frac.padEnd(right.frac.length, "0");
  const other = right.frac.padEnd(left.frac.length, "0");
  if (frac === other) {
    return 0;
  }
  return frac < other ? -1 : 1;
}
