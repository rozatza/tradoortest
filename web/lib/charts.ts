/**
 * Shape chart payloads for display.
 *
 * Counts and prices shown on the screen are the values in the payload.
 * This module does not compute an indicator, a return, or a bucket.
 */

import { isRecord } from "@/lib/backtest";

export const TRADINGVIEW_NOTICE =
  "TradingView Lightweight Charts™\nCopyright (с) 2025 TradingView, Inc. https://www.tradingview.com/";

export const TRADINGVIEW_LINK = "https://www.tradingview.com/";

const OVERLAY = new Set(["sma", "ema", "bollinger"]);
const MARKER_CAP = 500;

export type Candle = {
  open_time: number;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
  synthetic: boolean;
};

export type BarAsset = {
  assetId: string;
  total: number;
  shown: number;
  rejected: number;
  bars: Candle[];
  dropped: number;
};

export type BarsPayload =
  | { available: false; reason: string }
  | {
      available: true;
      barSize: string;
      slotsTotal: number;
      slotsShown: number;
      assets: BarAsset[];
    };

export type LinePoint = {
  time: number;
  value: number | null;
  valueText: string | null;
};

export type IndicatorSeries = {
  id: string;
  assetId: string;
  indicator: string;
  output: string | null;
  total: number;
  shown: number;
  points: LinePoint[];
  dropped: number;
  overlay: boolean;
};

export type IndicatorsPayload =
  | { available: false; reason: string }
  | { available: true; slotsTotal: number; slotsShown: number; series: IndicatorSeries[] };

export type HourBucket = { hourUtc: number; count: number };
export type WeekBucket = { weekdayUtc: number; weekday: string; count: number };

export type AnalysisPayload = {
  available: boolean;
  tradesTotal: number;
  tradesUsed: number;
  hours: HourBucket[];
  weekdays: WeekBucket[];
  dropped: number;
  reason: string | null;
};

export type TradeMarker = {
  time: number;
  timeText: string;
  kind: "entry" | "exit";
};

export function chartNotes(valid: number, considered: number, shown: number, total: number, noun: string): string[] {
  const notes: string[] = [];
  if (valid < considered) {
    notes.push(`${valid} of ${considered} points valid`);
  }
  if (shown < total) {
    notes.push(`${shown} of ${total} ${noun}`);
  }
  return notes;
}

export function parseBars(payload: unknown): BarsPayload | null {
  if (!isRecord(payload) || payload.schema !== "tradoortest.backtest_bars.v1") {
    return null;
  }
  if (payload.available === false) {
    return { available: false, reason: reasonText(payload.reason, "bars unavailable") };
  }
  if (payload.available !== true || typeof payload.bar_size !== "string" || !Array.isArray(payload.assets)) {
    return null;
  }
  const slotsTotal = whole(payload.slots_total);
  const slotsShown = whole(payload.slots_shown);
  if (slotsTotal === null || slotsShown === null) {
    return null;
  }
  const assets: BarAsset[] = [];
  for (const asset of payload.assets) {
    const parsed = parseAsset(asset);
    if (parsed) {
      assets.push(parsed);
    }
  }
  return {
    available: true,
    barSize: payload.bar_size,
    slotsTotal,
    slotsShown,
    assets,
  };
}

export function parseIndicators(payload: unknown): IndicatorsPayload | null {
  if (!isRecord(payload) || payload.schema !== "tradoortest.backtest_indicators.v1") {
    return null;
  }
  if (payload.available === false) {
    return { available: false, reason: reasonText(payload.reason, "indicators unavailable") };
  }
  if (payload.available !== true || !Array.isArray(payload.series)) {
    return null;
  }
  const slotsTotal = whole(payload.slots_total);
  const slotsShown = whole(payload.slots_shown);
  if (slotsTotal === null || slotsShown === null) {
    return null;
  }
  const series: IndicatorSeries[] = [];
  for (const item of payload.series) {
    const parsed = parseSeries(item);
    if (parsed) {
      series.push(parsed);
    }
  }
  return { available: true, slotsTotal, slotsShown, series };
}

export function parseAnalysis(payload: unknown): AnalysisPayload | null {
  if (!isRecord(payload) || payload.schema !== "tradoortest.backtest_analysis.v1") {
    return null;
  }
  const tradesTotal = whole(payload.trades_total);
  const tradesUsed = whole(payload.trades_used);
  if (tradesTotal === null || tradesUsed === null) {
    return null;
  }
  if (payload.available !== true) {
    return {
      available: false,
      tradesTotal,
      tradesUsed,
      hours: [],
      weekdays: [],
      dropped: 0,
      reason: typeof payload.reason === "string" && payload.reason !== "" ? payload.reason : null,
    };
  }
  let dropped = 0;
  const hours: HourBucket[] = [];
  const weekdays: WeekBucket[] = [];
  if (Array.isArray(payload.hours)) {
    for (const item of payload.hours) {
      const bucket = parseHour(item);
      if (bucket) {
        hours.push(bucket);
      } else {
        dropped += 1;
      }
    }
  } else {
    dropped += 1;
  }
  if (Array.isArray(payload.weekdays)) {
    for (const item of payload.weekdays) {
      const bucket = parseWeek(item);
      if (bucket) {
        weekdays.push(bucket);
      } else {
        dropped += 1;
      }
    }
  } else {
    dropped += 1;
  }
  return {
    available: hours.length > 0 || weekdays.length > 0,
    tradesTotal,
    tradesUsed,
    hours,
    weekdays,
    dropped,
    reason: null,
  };
}

export function markersFor(
  candles: readonly { open_time: number }[],
  trades: unknown,
  revealedThrough: number,
): { markers: TradeMarker[]; considered: number; shown: number } {
  const times = new Set(candles.map((candle) => candle.open_time));
  const markers: TradeMarker[] = [];
  let considered = 0;
  if (!Array.isArray(trades)) {
    return { markers, considered, shown: 0 };
  }
  for (const trade of trades) {
    if (!isRecord(trade)) {
      continue;
    }
    for (const kind of ["entry", "exit"] as const) {
      const raw = trade[kind === "entry" ? "entry_time" : "exit_time"];
      if (raw === null || raw === undefined || raw === "") {
        continue;
      }
      const time = exactUnix(raw);
      if (time !== null && time > revealedThrough) {
        continue;
      }
      considered += 1;
      if (time === null || !times.has(time) || markers.length >= MARKER_CAP) {
        continue;
      }
      markers.push({
        time,
        timeText: typeof raw === "string" ? raw : String(time),
        kind,
      });
    }
  }
  return { markers, considered, shown: markers.length };
}

export function replayIndex(index: number, length: number): number {
  if (length <= 0) {
    return 0;
  }
  if (!Number.isInteger(index) || index < 0) {
    return 0;
  }
  if (index >= length) {
    return length - 1;
  }
  return index;
}

export function revealedThrough(candles: readonly { open_time: number }[], index: number): number {
  if (candles.length === 0) {
    return 0;
  }
  return candles[replayIndex(index, candles.length)]!.open_time;
}

function parseAsset(asset: unknown): BarAsset | null {
  if (!isRecord(asset) || typeof asset.asset_id !== "string" || asset.asset_id === "") {
    return null;
  }
  const total = whole(asset.total);
  const shown = whole(asset.shown);
  const rejected = whole(asset.rejected);
  if (total === null || shown === null || rejected === null || !Array.isArray(asset.bars)) {
    return null;
  }
  const bars: Candle[] = [];
  let dropped = 0;
  for (const bar of asset.bars) {
    const candle = parseCandle(bar);
    if (candle) {
      bars.push(candle);
    } else {
      dropped += 1;
    }
  }
  bars.sort((left, right) => left.open_time - right.open_time);
  const unique: Candle[] = [];
  for (const candle of bars) {
    if (unique.length > 0 && unique[unique.length - 1]!.open_time === candle.open_time) {
      dropped += 1;
      continue;
    }
    unique.push(candle);
  }
  return {
    assetId: asset.asset_id,
    total,
    shown,
    rejected,
    bars: unique,
    dropped,
  };
}

function parseCandle(bar: unknown): Candle | null {
  if (!isRecord(bar) || typeof bar.synthetic !== "boolean") {
    return null;
  }
  const openTime = exactUnix(bar.open_time);
  const open = finiteSpell(bar.open);
  const high = finiteSpell(bar.high);
  const low = finiteSpell(bar.low);
  const close = finiteSpell(bar.close);
  const volume = finiteSpell(bar.volume);
  if (openTime === null || open === null || high === null || low === null || close === null || volume === null) {
    return null;
  }
  return { open_time: openTime, open, high, low, close, volume, synthetic: bar.synthetic };
}

function parseSeries(item: unknown): IndicatorSeries | null {
  if (!isRecord(item) || typeof item.id !== "string" || typeof item.asset_id !== "string") {
    return null;
  }
  if (typeof item.indicator !== "string" || !Array.isArray(item.points)) {
    return null;
  }
  const total = whole(item.total);
  const shown = whole(item.shown);
  if (total === null || shown === null) {
    return null;
  }
  const output = item.output === null ? null : typeof item.output === "string" ? item.output : null;
  if (item.output !== null && typeof item.output !== "string") {
    return null;
  }
  const points: LinePoint[] = [];
  let dropped = 0;
  for (const point of item.points) {
    const parsed = parsePoint(point);
    if (parsed) {
      points.push(parsed);
    } else {
      dropped += 1;
    }
  }
  points.sort((left, right) => left.time - right.time);
  return {
    id: item.id,
    assetId: item.asset_id,
    indicator: item.indicator,
    output,
    total,
    shown,
    points,
    dropped,
    overlay: OVERLAY.has(item.indicator),
  };
}

function parsePoint(point: unknown): LinePoint | null {
  if (!isRecord(point)) {
    return null;
  }
  const time = exactUnix(point.open_time);
  if (time === null) {
    return null;
  }
  if (point.value === null) {
    return { time, value: null, valueText: null };
  }
  const valueText = finiteSpell(point.value);
  if (valueText === null) {
    return null;
  }
  return { time, value: Number(valueText), valueText };
}

function parseHour(item: unknown): HourBucket | null {
  if (!isRecord(item)) {
    return null;
  }
  const hourUtc = whole(item.hour_utc);
  const count = whole(item.count);
  if (hourUtc === null || count === null || hourUtc > 23 || count < 1) {
    return null;
  }
  return { hourUtc, count };
}

function parseWeek(item: unknown): WeekBucket | null {
  if (!isRecord(item) || typeof item.weekday !== "string" || item.weekday === "") {
    return null;
  }
  const weekdayUtc = whole(item.weekday_utc);
  const count = whole(item.count);
  if (weekdayUtc === null || count === null || weekdayUtc > 6 || count < 1) {
    return null;
  }
  return { weekdayUtc, weekday: item.weekday, count };
}

function whole(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    return null;
  }
  return value;
}

function finiteSpell(value: unknown): string | null {
  if (typeof value !== "string" || value === "") {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  return value;
}

/** The trade-log timestamp as an integer second, or null when it is not that integer. */
export function exactUnix(value: unknown): number | null {
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value) || value < 0) {
      return null;
    }
    return value;
  }
  if (typeof value !== "string" || !/^[0-9]+$/.test(value)) {
    return null;
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || String(parsed) !== value) {
    return null;
  }
  return parsed;
}

function reasonText(value: unknown, fallback: string): string {
  if (typeof value === "string" && value !== "") {
    return value;
  }
  return fallback;
}
