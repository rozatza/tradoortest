/**
 * Paper dashboard view. P&L is the stored realized text plus, when the
 * position is open, qty × mark − basis. Nothing else is priced.
 */

export type PaperMark = {
  label: "mark";
  price: string;
  openTime: number;
  timeframe: string;
  source: "last_stored_close";
};

export type PaperStrategy = {
  strategyId: string;
  version: number;
  name: string | null;
  barSize: string;
  registeredAt: number;
  status: "running";
};

export type PaperPosition = {
  strategyId: string;
  version: number;
  assetId: string;
  state: "open" | "flat" | "open_unmanaged";
  qty: string | null;
  entry: string | null;
  basis: string | null;
  realized: string;
  mark: PaperMark | null;
};

export type PaperEvent = {
  strategyId: string;
  version: number;
  openTime: number;
  kind: string;
  detail: string;
};

export type PaperSnapshot = {
  mode: "paper";
  simulated: true;
  strategies: PaperStrategy[];
  positions: PaperPosition[];
  events: PaperEvent[];
};

export type PaperPnl = {
  realized: string;
  markPrice: string | null;
  combined: string | null;
  combinedLabel: "stored realized + mark" | null;
};

type Rational = { n: bigint; d: bigint };

const HEX_ID = /^[0-9a-f]{32}$/;
const VERSION = /^[1-9][0-9]{0,6}$/;
const FRACTION = /^-?\d+\/[1-9]\d*$/;
const DECIMAL = /^-?\d+(\.\d+)?$/;
const MAX_DIGITS = 200;
const ZERO = BigInt(0);
const TEN = BigInt(10);

export function paperDeployPath(strategyId: string, version: string): string | null {
  if (!HEX_ID.test(strategyId) || !VERSION.test(version)) {
    return null;
  }
  const parsed = Number(version);
  if (parsed < 1 || parsed > 1_000_000) {
    return null;
  }
  return `/api/strategies/${strategyId}/versions/${version}/paper`;
}

export function readSnapshot(value: unknown): PaperSnapshot {
  if (!isRecord(value) || value.mode !== "paper" || value.simulated !== true) {
    throw new Error("paper snapshot is invalid");
  }
  return {
    mode: "paper",
    simulated: true,
    strategies: readList(value.strategies, readStrategy),
    positions: readList(value.positions, readPosition),
    events: readList(value.events, readEvent),
  };
}

/**
 * Stored realized, plus qty × mark − basis when that position is still open.
 * A flat book returns the stored realized only. A missing basis or mark
 * leaves the combined figure empty.
 */
export function paperPnl(position: PaperPosition): PaperPnl {
  const markPrice = position.mark?.price ?? null;
  const qtyText = position.qty;
  const basisText = position.basis;
  const mark = position.mark;
  if (position.state === "flat" || qtyText === null || basisText === null || mark === null) {
    return { realized: position.realized, markPrice, combined: null, combinedLabel: null };
  }
  const realized = parseFraction(position.realized);
  const qty = parseDecimal(qtyText);
  const basis = parseFraction(basisText);
  const marked = parseDecimal(mark.price);
  if (realized === null || qty === null || basis === null || marked === null) {
    return { realized: position.realized, markPrice, combined: null, combinedLabel: null };
  }
  const combined = add(realized, sub(mul(qty, marked), basis));
  return {
    realized: position.realized,
    markPrice,
    combined: `${combined.n}/${combined.d}`,
    combinedLabel: "stored realized + mark",
  };
}

export function errorMessage(value: unknown, fallback: string): string {
  if (!isRecord(value) || !isRecord(value.error)) {
    return fallback;
  }
  const message = value.error.message;
  if (typeof message === "string" && message !== "") {
    return message;
  }
  return fallback;
}

function readStrategy(value: unknown): PaperStrategy {
  if (!isRecord(value) || value.status !== "running") {
    throw new Error("paper snapshot is invalid");
  }
  const name = value.name;
  return {
    strategyId: readId(value.strategy_id),
    version: readCount(value.version),
    name: name === null ? null : readText(name),
    barSize: readText(value.bar_size),
    registeredAt: readCount(value.registered_at),
    status: "running",
  };
}

function readPosition(value: unknown): PaperPosition {
  if (!isRecord(value)) {
    throw new Error("paper snapshot is invalid");
  }
  const state = value.state;
  if (state !== "open" && state !== "flat" && state !== "open_unmanaged") {
    throw new Error("paper snapshot is invalid");
  }
  return {
    strategyId: readId(value.strategy_id),
    version: readCount(value.version),
    assetId: readText(value.asset_id),
    state,
    qty: readOptionalText(value.qty),
    entry: readOptionalText(value.entry),
    basis: readOptionalText(value.basis),
    realized: readText(value.realized),
    mark: readMark(value.mark),
  };
}

function readEvent(value: unknown): PaperEvent {
  if (!isRecord(value)) {
    throw new Error("paper snapshot is invalid");
  }
  return {
    strategyId: readId(value.strategy_id),
    version: readCount(value.version),
    openTime: readCount(value.open_time),
    kind: readText(value.kind),
    detail: readText(value.detail),
  };
}

function readMark(value: unknown): PaperMark | null {
  if (value === null) {
    return null;
  }
  if (!isRecord(value) || value.label !== "mark" || value.source !== "last_stored_close") {
    throw new Error("paper snapshot is invalid");
  }
  return {
    label: "mark",
    price: readText(value.price),
    openTime: readCount(value.open_time),
    timeframe: readText(value.timeframe),
    source: "last_stored_close",
  };
}

function readList<T>(value: unknown, read: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) {
    throw new Error("paper snapshot is invalid");
  }
  return value.map(read);
}

function readId(value: unknown): string {
  if (typeof value !== "string" || !HEX_ID.test(value)) {
    throw new Error("paper snapshot is invalid");
  }
  return value;
}

function readText(value: unknown): string {
  if (typeof value !== "string" || value === "") {
    throw new Error("paper snapshot is invalid");
  }
  return value;
}

function readOptionalText(value: unknown): string | null {
  if (value === null) {
    return null;
  }
  return readText(value);
}

function readCount(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new Error("paper snapshot is invalid");
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseFraction(text: string): Rational | null {
  if (text.length > MAX_DIGITS || !FRACTION.test(text)) {
    return null;
  }
  const [num, den] = text.split("/");
  if (num === undefined || den === undefined) {
    return null;
  }
  return reduce(BigInt(num), BigInt(den));
}

function parseDecimal(text: string): Rational | null {
  if (text.length > MAX_DIGITS || !DECIMAL.test(text)) {
    return null;
  }
  const negative = text.startsWith("-");
  const body = negative ? text.slice(1) : text;
  const [whole, frac = ""] = body.split(".");
  const digits = `${whole ?? ""}${frac}`;
  if (digits === "") {
    return null;
  }
  const scale = TEN ** BigInt(frac.length);
  const magnitude = BigInt(digits);
  return reduce(negative ? -magnitude : magnitude, scale);
}

function add(left: Rational, right: Rational): Rational {
  return reduce(left.n * right.d + right.n * left.d, left.d * right.d);
}

function sub(left: Rational, right: Rational): Rational {
  return add(left, { n: -right.n, d: right.d });
}

function mul(left: Rational, right: Rational): Rational {
  return reduce(left.n * right.n, left.d * right.d);
}

function reduce(numerator: bigint, denominator: bigint): Rational {
  if (denominator < ZERO) {
    return reduce(-numerator, -denominator);
  }
  const divisor = gcd(numerator < ZERO ? -numerator : numerator, denominator);
  return { n: numerator / divisor, d: denominator / divisor };
}

function gcd(left: bigint, right: bigint): bigint {
  let a = left;
  let b = right;
  while (b !== ZERO) {
    const next = a % b;
    a = b;
    b = next;
  }
  return a;
}
