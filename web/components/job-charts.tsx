"use client";

import { useEffect, useState } from "react";

import { AnalysisCharts } from "@/components/analysis-charts";
import { PriceChart } from "@/components/price-chart";
import { BacktestRequestError, fetchJobJson } from "@/lib/backtest";
import { parseAnalysis, parseBars, parseIndicators, type AnalysisPayload, type BarsPayload, type IndicatorsPayload } from "@/lib/charts";

type Load = {
  bars: BarsPayload | null;
  indicators: IndicatorsPayload | null;
  analysis: AnalysisPayload | null;
};

export function JobCharts({ jobId, trades }: { jobId: string; trades: unknown }) {
  const [load, setLoad] = useState<Load | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [pending, setPending] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPending(true);
    setErrors([]);
    void (async () => {
      const [bars, indicators, analysis] = await Promise.allSettled([
        fetchJobJson(jobId, "/bars"),
        fetchJobJson(jobId, "/indicators"),
        fetchJobJson(jobId, "/analysis"),
      ]);
      if (cancelled) {
        return;
      }
      const nextErrors: string[] = [];
      const next: Load = { bars: null, indicators: null, analysis: null };
      if (bars.status === "fulfilled") {
        next.bars = parseBars(bars.value);
      } else {
        nextErrors.push(`bars: ${failureLine(bars.reason)}`);
      }
      if (indicators.status === "fulfilled") {
        next.indicators = parseIndicators(indicators.value);
      } else {
        nextErrors.push(`indicators: ${failureLine(indicators.reason)}`);
      }
      if (analysis.status === "fulfilled") {
        next.analysis = parseAnalysis(analysis.value);
      } else {
        nextErrors.push(`analysis: ${failureLine(analysis.reason)}`);
      }
      setLoad(next);
      setErrors(nextErrors);
      setPending(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [jobId, attempt]);

  return (
    <div className="chart-block">
      <h3>Price chart</h3>
      {pending ? <p role="status">Loading charts…</p> : null}
      {errors.length > 0 ? (
        <div>
          {errors.map((error) => (
            <p key={error} className="alert" role="alert">
              {error}
            </p>
          ))}
          <button type="button" className="secondary" onClick={() => setAttempt((current) => current + 1)}>
            Retry charts
          </button>
        </div>
      ) : null}
      {load?.bars && !load.bars.available ? <p>{load.bars.reason}</p> : null}
      {load?.bars && load.bars.available ? (
        <PriceChart bars={load.bars} indicators={load.indicators} trades={trades} />
      ) : null}
      {!pending && !load?.bars ? <p>Price chart unavailable</p> : null}
      {load?.analysis ? <AnalysisCharts analysis={load.analysis} /> : null}
      {!pending && !load?.analysis ? (
        <>
          <h3>Time of day</h3>
          <p>Time of day unavailable</p>
          <h3>Weekday</h3>
          <p>Weekday unavailable</p>
        </>
      ) : null}
    </div>
  );
}

function failureLine(error: unknown): string {
  if (error instanceof BacktestRequestError) {
    return `${error.code}: ${error.message}`;
  }
  return "network_error: the request could not be completed";
}
