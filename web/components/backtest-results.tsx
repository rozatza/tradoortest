"use client";

import { useState, type FormEvent } from "react";

import { JobCharts } from "@/components/job-charts";
import {
  BacktestRequestError,
  DOWNLOADS,
  EQUITY_DRAW_CAP,
  METRIC_FACTS,
  METRIC_FIELDS,
  TRADE_COLUMNS,
  TRADE_PAGE_SIZE,
  UNAVAILABLE,
  curveCoordinates,
  downloadBacktest,
  isEquityPoint,
  isJobId,
  isRecord,
  loadBacktest,
  markText,
  pointsForCurve,
  rawText,
  tradePage,
  type DownloadKind,
  type LoadedBacktest,
} from "@/lib/backtest";

export function BacktestResults() {
  const [jobId, setJobId] = useState("");
  const [loaded, setLoaded] = useState<LoadedBacktest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onLoad(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = jobId.trim();
    setExportError(null);
    setLoaded(null);
    if (!isJobId(id)) {
      setError("invalid_id: job_id is invalid");
      return;
    }
    setPending(true);
    setError(null);
    try {
      setLoaded(await loadBacktest(id));
    } catch (caught) {
      setLoaded(null);
      setError(failureLine(caught));
    } finally {
      setPending(false);
    }
  }

  async function onDownload(kind: DownloadKind) {
    if (!loaded || loaded.kind !== "ready") {
      return;
    }
    setExportError(null);
    try {
      await downloadBacktest(loaded.jobId, kind);
    } catch (caught) {
      setExportError(failureLine(caught));
    }
  }

  return (
    <section className="results" aria-label="Backtest results">
      <h2>Backtest results</h2>
      <p className="lede">Loaded from the job API. This screen does not trade.</p>
      <form className="results-form" onSubmit={onLoad}>
        <label htmlFor="backtest-job-id">Job id</label>
        <input
          id="backtest-job-id"
          name="job_id"
          value={jobId}
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setJobId(event.target.value)}
        />
        <button type="submit" disabled={pending}>
          {pending ? "Loading…" : "Load results"}
        </button>
      </form>
      {error ? (
        <p className="alert" role="alert">
          {error}
        </p>
      ) : null}
      {loaded?.kind === "pending" ? (
        <p role="status">
          Backtest is {loaded.status}. Results are not ready.
        </p>
      ) : null}
      {loaded?.kind === "ready" ? (
        <ResultView
          key={loaded.jobId}
          jobId={loaded.jobId}
          result={loaded.result}
          exportError={exportError}
          onDownload={onDownload}
        />
      ) : null}
    </section>
  );
}

function ResultView({
  jobId,
  result,
  exportError,
  onDownload,
}: {
  jobId: string;
  result: Record<string, unknown>;
  exportError: string | null;
  onDownload: (kind: DownloadKind) => void;
}) {
  const schema = rawText(result.schema);
  const finalEquity = rawText(result.final_equity);
  const finalCash = rawText(result.final_cash);
  return (
    <div className="result-body">
      <p className="meta">
        <span>
          Schema <span title={schema.title}>{schema.shown}</span>
        </span>
        <span>
          Final equity <span title={finalEquity.title}>{finalEquity.shown}</span>
        </span>
        <span>
          Final cash <span title={finalCash.title}>{finalCash.shown}</span>
        </span>
      </p>
      <EquityChart curve={result.equity_curve} />
      <JobCharts jobId={jobId} trades={result.trades} />
      <h3>Metrics</h3>
      <Metrics metrics={result.metrics} />
      <h3>Lints</h3>
      <Lints lints={result.lints} />
      <h3>Trade log</h3>
      <TradeLog trades={result.trades} />
      <div className="actions">
        {DOWNLOADS.map((item) => (
          <button key={item.kind} type="button" className="secondary" onClick={() => onDownload(item.kind)}>
            {item.label}
          </button>
        ))}
      </div>
      {exportError ? (
        <p className="alert" role="alert">
          {exportError}
        </p>
      ) : null}
    </div>
  );
}

function EquityChart({ curve }: { curve: unknown }) {
  if (!Array.isArray(curve) || curve.length === 0) {
    return <p>Equity curve unavailable</p>;
  }
  const points = curve.filter(isEquityPoint);
  if (points.length === 0) {
    return <p>Equity curve unavailable</p>;
  }
  const drawn = pointsForCurve(points, EQUITY_DRAW_CAP);
  const coords = curveCoordinates(drawn);
  const first = markText(points[0]!.equity);
  const last = markText(points[points.length - 1]!.equity);
  const notes: string[] = [];
  if (points.length < curve.length) {
    notes.push(`${points.length} of ${curve.length} points valid`);
  }
  if (drawn.length < points.length) {
    notes.push(`${drawn.length} of ${points.length} points drawn`);
  }
  return (
    <figure className="equity">
      {coords ? (
        <svg
          className="equity-chart"
          role="img"
          aria-label={`Equity curve from ${first} to ${last}`}
          viewBox="0 0 100 40"
          preserveAspectRatio="none"
          data-drawn={drawn.length}
          data-total={curve.length}
        >
          <title>{`${first} ${last}`}</title>
          {drawn.length === 1 ? (
            <circle cx="50" cy="20" r="1.2" fill="#1f6b3a" />
          ) : (
            <polyline points={coords} fill="none" stroke="#1f6b3a" strokeWidth="1.5" />
          )}
        </svg>
      ) : (
        <p>Equity curve unavailable</p>
      )}
      <figcaption>
        <span title={first}>{first}</span>
        {" to "}
        <span title={last}>{last}</span>
        {notes.length > 0 ? ` · ${notes.join(" · ")}` : null}
      </figcaption>
    </figure>
  );
}

function Metrics({ metrics }: { metrics: unknown }) {
  const record = isRecord(metrics) ? metrics : null;
  const values = record && isRecord(record.values) ? record.values : null;
  const reasons = record && isRecord(record.undefined) ? record.undefined : null;
  return (
    <dl className="metrics">
      <FactRows fields={METRIC_FIELDS} source={values} reasons={reasons} />
      <FactRows fields={METRIC_FACTS} source={record} />
    </dl>
  );
}

function FactRows({
  fields,
  source,
  reasons,
}: {
  fields: readonly string[];
  source: Record<string, unknown> | null;
  reasons?: Record<string, unknown> | null;
}) {
  return (
    <>
      {fields.map((field) => {
        const present = source !== null && Object.hasOwn(source, field);
        const value = rawText(present ? source[field] : undefined);
        const raw = present ? source[field] : undefined;
        const reasonText =
          reasons && (raw === null || raw === undefined) && Object.hasOwn(reasons, field)
            ? rawText(reasons[field])
            : null;
        return (
          <div key={field}>
            <dt>{field}</dt>
            <dd title={value.title}>{value.shown}</dd>
            {reasonText && reasonText.shown !== UNAVAILABLE ? (
              <dd title={reasonText.title}>{reasonText.shown}</dd>
            ) : null}
          </div>
        );
      })}
    </>
  );
}

function Lints({ lints }: { lints: unknown }) {
  if (!Array.isArray(lints)) {
    return <p>Lints unavailable</p>;
  }
  if (lints.length === 0) {
    return <p>No lints</p>;
  }
  return (
    <ul className="lints">
      {lints.map((lint, index) => {
        const text = rawText(lint);
        return (
          <li key={`${text.shown}-${index}`} title={text.title}>
            {text.shown}
          </li>
        );
      })}
    </ul>
  );
}

function TradeLog({ trades }: { trades: unknown }) {
  const [page, setPage] = useState(0);
  if (!Array.isArray(trades)) {
    return <p>Trade log unavailable</p>;
  }
  const start = page * TRADE_PAGE_SIZE;
  const visible = tradePage(trades, page);
  const end = start + visible.length;
  return (
    <div>
      <p className="status-line">
        {trades.length === 0 ? "0 trades" : `${start + 1}-${end} of ${trades.length}`}
      </p>
      <div className="trade-scroll">
        <table className="trade-log">
          <thead>
            <tr>
              {TRADE_COLUMNS.map((column) => (
                <th key={column} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((trade, index) => (
              <TradeRow key={`${start + index}`} trade={trade} />
            ))}
          </tbody>
        </table>
      </div>
      <div className="actions">
        <button type="button" className="secondary" onClick={() => setPage((current) => Math.max(0, current - 1))} disabled={page === 0}>
          Previous
        </button>
        <button
          type="button"
          className="secondary"
          onClick={() => setPage((current) => current + 1)}
          disabled={end >= trades.length}
        >
          Next
        </button>
      </div>
    </div>
  );
}

function TradeRow({ trade }: { trade: unknown }) {
  if (!isRecord(trade)) {
    return (
      <tr>
        <td colSpan={TRADE_COLUMNS.length}>{UNAVAILABLE}</td>
      </tr>
    );
  }
  return (
    <tr>
      {TRADE_COLUMNS.map((column) => {
        const text = rawText(Object.hasOwn(trade, column) ? trade[column] : undefined);
        return (
          <td key={column} title={text.title}>
            {text.shown}
          </td>
        );
      })}
    </tr>
  );
}

function failureLine(error: unknown): string {
  if (error instanceof BacktestRequestError) {
    return `${error.code}: ${error.message}`;
  }
  return "network_error: the request could not be completed";
}
