"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import {
  TRADINGVIEW_LINK,
  TRADINGVIEW_NOTICE,
  chartNotes,
  markersFor,
  replayIndex,
  revealedThrough,
  type BarsPayload,
  type Candle,
  type IndicatorSeries,
  type IndicatorsPayload,
  type TradeMarker,
} from "@/lib/charts";

const OVERLAY_COLORS = ["#1d4e89", "#8a5a00", "#6b3fa0", "#0f6e6e"];
const SEPARATE_COLORS = ["#8a2e24", "#1f6b3a", "#1d4e89", "#6b3fa0"];

type ReadyBars = Extract<BarsPayload, { available: true }>;

export function PriceChart({
  bars,
  indicators,
  trades,
}: {
  bars: ReadyBars;
  indicators: IndicatorsPayload | null;
  trades: unknown;
}) {
  const [assetId, setAssetId] = useState(bars.assets[0]?.assetId ?? "");
  const asset = bars.assets.find((item) => item.assetId === assetId) ?? bars.assets[0];
  const [index, setIndex] = useState(0);
  useEffect(() => {
    setIndex(0);
  }, [asset?.assetId]);

  if (!asset || asset.bars.length === 0) {
    return <p>Price chart unavailable</p>;
  }
  const at = replayIndex(index, asset.bars.length);
  const revealed = asset.bars.slice(0, at + 1);
  const current = revealed[revealed.length - 1]!;
  const through = revealedThrough(asset.bars, at);
  const placed = markersFor(asset.bars, trades, through);
  const series =
    indicators && indicators.available
      ? indicators.series.filter((item) => item.assetId === asset.assetId)
      : [];
  const overlays = series.filter((item) => item.overlay);
  const separate = series.filter((item) => !item.overlay);
  const candleNotes = chartNotes(
    asset.bars.length,
    asset.bars.length + asset.dropped + asset.rejected,
    asset.shown,
    asset.total,
    "bars shown",
  );
  const markerNote =
    placed.shown < placed.considered ? `${placed.shown} of ${placed.considered} markers shown` : null;

  return (
    <figure className="chart-block">
      <figcaption className="bar-readout">
        {bars.assets.length > 1 ? (
          <label>
            Asset{" "}
            <select
              value={asset.assetId}
              onChange={(event) => setAssetId(event.target.value)}
            >
              {bars.assets.map((item) => (
                <option key={item.assetId} value={item.assetId}>
                  {item.assetId}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <span>{asset.assetId}</span>
        )}
        {` · ${bars.barSize} · Bar ${at + 1} of ${asset.bars.length}`}
        {current.synthetic ? " · synthetic" : ""}
        {` · open ${current.open} high ${current.high} low ${current.low} close ${current.close} volume ${current.volume}`}
        {candleNotes.length ? ` · ${candleNotes.join(" · ")}` : ""}
        {markerNote ? ` · ${markerNote}` : ""}
      </figcaption>
      <div className="chart-controls">
        <button type="button" className="secondary" onClick={() => setIndex(0)} disabled={at === 0}>
          First bar
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => setIndex(at - 1)}
          disabled={at === 0}
        >
          Step back
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => setIndex(at + 1)}
          disabled={at >= asset.bars.length - 1}
        >
          Step forward
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => setIndex(asset.bars.length - 1)}
          disabled={at >= asset.bars.length - 1}
        >
          Last bar
        </button>
      </div>
      <CandleCanvas candles={revealed} overlays={overlays} markers={placed.markers} />
      {overlays.map((item) => (
        <SeriesNote key={item.id} series={item} through={through} />
      ))}
      {separate.map((item, offset) => (
        <IndicatorPane key={`${item.id}:${item.output ?? ""}`} series={item} through={through} color={SEPARATE_COLORS[offset % SEPARATE_COLORS.length]!} />
      ))}
      {indicators && !indicators.available ? <p>{indicators.reason}</p> : null}
      {indicators && indicators.available && series.length === 0 ? <p>No indicators</p> : null}
      {placed.markers.length > 0 ? (
        <ul className="marker-list">
          {placed.markers.map((marker) => (
            <li key={`${marker.kind}-${marker.time}`} data-marker-time={marker.time} data-marker-kind={marker.kind}>
              {marker.kind} {marker.timeText}
            </li>
          ))}
        </ul>
      ) : null}
      <p className="attribution">
        {TRADINGVIEW_NOTICE.split("\n").map((line) => (
          <span key={line}>
            {line.includes(TRADINGVIEW_LINK) ? (
              <>
                {line.slice(0, line.indexOf(TRADINGVIEW_LINK))}
                <a href={TRADINGVIEW_LINK} target="_blank" rel="noreferrer">
                  {TRADINGVIEW_LINK}
                </a>
              </>
            ) : (
              line
            )}
            <br />
          </span>
        ))}
      </p>
    </figure>
  );
}

function SeriesNote({ series, through }: { series: IndicatorSeries; through: number }) {
  const note = seriesNote(series, through);
  return (
    <p className="bar-readout">
      {seriesLabel(series)}
      {note.caption ? ` · ${note.caption}` : ""}
      {note.notes.length ? ` · ${note.notes.join(" · ")}` : ""}
    </p>
  );
}

function IndicatorPane({
  series,
  through,
  color,
}: {
  series: IndicatorSeries;
  through: number;
  color: string;
}) {
  const note = seriesNote(series, through);
  const revealed = useMemo(
    () => series.points.filter((point) => point.time <= through),
    [series.points, through],
  );
  return (
    <div className="chart-block">
      <p className="bar-readout">
        {seriesLabel(series)}
        {note.caption ? ` · ${note.caption}` : ""}
        {note.notes.length ? ` · ${note.notes.join(" · ")}` : ""}
      </p>
      <LineCanvas points={revealed} color={color} />
    </div>
  );
}

function seriesNote(series: IndicatorSeries, through: number): { caption: string | null; notes: string[] } {
  const finite = series.points.filter((point) => point.valueText !== null && point.time <= through);
  const inWindow = series.points.filter((point) => point.time <= through);
  const notes = chartNotes(
    finite.length,
    inWindow.length + series.dropped,
    series.shown,
    series.total,
    "points shown",
  );
  const first = finite[0]?.valueText ?? null;
  const last = finite[finite.length - 1]?.valueText ?? null;
  const caption = first && last ? `${first} to ${last}` : null;
  return { caption, notes };
}

function seriesLabel(series: IndicatorSeries): string {
  return series.output ? `${series.id} ${series.output}` : series.id;
}

type ChartHandle = {
  addSeries: (definition: unknown, options: object) => { setData: (data: object[]) => void };
  remove: () => void;
  timeScale: () => { fitContent: () => void };
};

type ChartsLib = {
  createChart: (element: HTMLElement, options: object) => ChartHandle;
  CandlestickSeries: unknown;
  LineSeries: unknown;
  createSeriesMarkers: (series: unknown, markers: object[]) => void;
};

function CandleCanvas({
  candles,
  overlays,
  markers,
}: {
  candles: Candle[];
  overlays: IndicatorSeries[];
  markers: TradeMarker[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const candleKey = candles.map((candle) => `${candle.open_time}:${candle.close}`).join(",");
  const markerKey = markers.map((marker) => `${marker.kind}:${marker.time}`).join(",");
  const overlayKey = overlays.map((series) => series.id).join(",");
  useEffect(() => {
    const host = ref.current;
    if (!host) {
      return;
    }
    let cancelled = false;
    let chart: ChartHandle | null = null;
    setError(null);
    void (async () => {
      try {
        const lib = (await import("lightweight-charts")) as ChartsLib;
        if (cancelled || !ref.current) {
          return;
        }
        chart = lib.createChart(ref.current, chartOptions());
        const series = chart.addSeries(lib.CandlestickSeries, {
          upColor: "#1f6b3a",
          downColor: "#8a2e24",
          borderUpColor: "#1f6b3a",
          borderDownColor: "#8a2e24",
          wickUpColor: "#1f6b3a",
          wickDownColor: "#8a2e24",
        });
        const last = candles[candles.length - 1]?.open_time ?? 0;
        series.setData(
          candles.map((candle) => ({
            time: candle.open_time,
            open: Number(candle.open),
            high: Number(candle.high),
            low: Number(candle.low),
            close: Number(candle.close),
          })),
        );
        overlays.forEach((line, offset) => {
          const added = chart!.addSeries(lib.LineSeries, {
            color: OVERLAY_COLORS[offset % OVERLAY_COLORS.length],
            lineWidth: 2,
            priceLineVisible: false,
            lastValueVisible: false,
          });
          added.setData(lineData(line, last));
        });
        lib.createSeriesMarkers(
          series,
          markers.map((marker) => ({
            time: marker.time,
            position: marker.kind === "entry" ? "belowBar" : "aboveBar",
            color: marker.kind === "entry" ? "#1f6b3a" : "#8a2e24",
            shape: marker.kind === "entry" ? "arrowUp" : "arrowDown",
            text: marker.kind,
          })),
        );
        chart.timeScale().fitContent();
      } catch {
        if (!cancelled) {
          setError("the price chart could not be drawn");
        }
      }
    })();
    return () => {
      cancelled = true;
      chart?.remove();
    };
  }, [attempt, candleKey, markerKey, overlayKey]);
  return (
    <div className="chart-block">
      <div ref={ref} className="price-pane" data-replay-bars={candles.length} />
      {error ? (
        <p className="alert" role="alert">
          {error}{" "}
          <button type="button" className="secondary" onClick={() => setAttempt((current) => current + 1)}>
            Retry chart
          </button>
        </p>
      ) : null}
    </div>
  );
}

function LineCanvas({ points, color }: { points: IndicatorSeries["points"]; color: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const key = points.map((point) => `${point.time}:${point.valueText ?? ""}`).join(",");
  useEffect(() => {
    const host = ref.current;
    if (!host) {
      return;
    }
    let cancelled = false;
    let chart: ChartHandle | null = null;
    setError(null);
    void (async () => {
      try {
        const lib = (await import("lightweight-charts")) as ChartsLib;
        if (cancelled || !ref.current) {
          return;
        }
        chart = lib.createChart(ref.current, chartOptions());
        const series = chart.addSeries(lib.LineSeries, {
          color,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        series.setData(
          points.map((point) =>
            point.value === null ? { time: point.time } : { time: point.time, value: point.value },
          ),
        );
        chart.timeScale().fitContent();
      } catch {
        if (!cancelled) {
          setError("the indicator chart could not be drawn");
        }
      }
    })();
    return () => {
      cancelled = true;
      chart?.remove();
    };
  }, [attempt, color, key]);
  return (
    <div>
      <div ref={ref} className="indicator-pane" />
      {error ? (
        <p className="alert" role="alert">
          {error}{" "}
          <button type="button" className="secondary" onClick={() => setAttempt((current) => current + 1)}>
            Retry chart
          </button>
        </p>
      ) : null}
    </div>
  );
}

function lineData(series: IndicatorSeries, through: number): object[] {
  return series.points
    .filter((point) => point.time <= through)
    .map((point) => (point.value === null ? { time: point.time } : { time: point.time, value: point.value }));
}

function chartOptions(): object {
  return {
    autoSize: true,
    layout: {
      background: { color: "#f7f8f5" },
      textColor: "#1c2118",
    },
    grid: {
      vertLines: { color: "#e1e6dc" },
      horzLines: { color: "#e1e6dc" },
    },
    rightPriceScale: { borderColor: "#d5ddd0" },
    timeScale: { borderColor: "#d5ddd0" },
  };
}
