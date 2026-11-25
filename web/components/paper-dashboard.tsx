"use client";

import { useEffect, useState, type FormEvent } from "react";

import {
  EMPTY_SHADOW,
  PAPER_EVENTS_LIMIT,
  errorMessage,
  formatPaperAmount,
  paperDeployPath,
  paperPnl,
  readSnapshot,
  type PaperDrift,
  type PaperEvent,
  type PaperSnapshot,
  type ShadowReport,
} from "@/lib/paper";

const EMPTY: PaperSnapshot = {
  mode: "paper",
  simulated: true,
  strategies: [],
  positions: [],
  events: [],
  eventsCursor: 0,
  eventsHasMore: false,
  driftThreshold: "0.01",
  driftThresholdName: "PAPER_DRIFT_THRESHOLD",
  drift: [],
  shadow: EMPTY_SHADOW,
};

export function PaperDashboard({ pollMs = 2000 }: { pollMs?: number }) {
  const [snapshot, setSnapshot] = useState<PaperSnapshot>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [strategyId, setStrategyId] = useState("");
  const [version, setVersion] = useState("1");
  const [busy, setBusy] = useState(false);
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    let since: number | null = null;
    const pull = async () => {
      let more = true;
      let pages = 0;
      while (more && pages < 5 && !cancelled) {
        pages += 1;
        const sentSince = since;
        const params = new URLSearchParams({ events_limit: String(PAPER_EVENTS_LIMIT) });
        if (sentSince !== null) {
          params.set("events_since", String(sentSince));
        }
        try {
          const response = await fetch(`/api/paper?${params.toString()}`, {
            cache: "no-store",
            headers: { accept: "application/json" },
          });
          const body: unknown = await response.json();
          if (cancelled) {
            return;
          }
          if (!response.ok) {
            setError(errorMessage(body, "the paper API is unavailable"));
            more = false;
          } else {
            const next = readSnapshot(body);
            if (sentSince === null) {
              setSnapshot(next);
            } else {
              setSnapshot((current) => ({
                ...next,
                events: mergeEvents(current.events, next.events),
                eventsHasMore: current.eventsHasMore || next.eventsHasMore,
              }));
            }
            setError(null);
            since = next.eventsCursor;
            more = sentSince !== null && next.eventsHasMore;
          }
        } catch {
          if (!cancelled) {
            setError("the paper API is unavailable");
          }
          more = false;
        }
      }
      if (!cancelled) {
        timer = window.setTimeout(() => {
          void pull();
        }, pollMs);
      }
    };
    void pull();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [pollMs, generation]);

  async function deploy(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const path = paperDeployPath(strategyId.trim(), version.trim());
    if (path === null) {
      setError("strategy id or version is invalid");
      return;
    }
    await send(path, "POST");
  }

  async function undeploy(id: string, number: number) {
    const path = paperDeployPath(id, String(number));
    if (path === null) {
      setError("strategy id or version is invalid");
      return;
    }
    await send(path, "DELETE");
  }

  async function send(path: string, method: "POST" | "DELETE") {
    setBusy(true);
    try {
      const response = await fetch(path, {
        method,
        headers: { accept: "application/json", "content-type": "application/json" },
        body: method === "POST" ? "{}" : undefined,
      });
      const body: unknown = await response.json().catch(() => null);
      if (!response.ok) {
        setError(errorMessage(body, "the paper API is unavailable"));
        return;
      }
      setError(null);
      setGeneration((current) => current + 1);
    } catch {
      setError("the paper API is unavailable");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="paper-grid">
      <p className="paper-banner">PAPER / simulated. No live order is sent.</p>
      {error ? (
        <p className="alert" role="alert">
          {error}
        </p>
      ) : null}
      <PaperBook
        snapshot={snapshot}
        busy={busy}
        onUndeploy={(id, number) => void undeploy(id, number)}
      />
      <section className="card" aria-label="Deploy on PAPER">
        <h2>Deploy on PAPER</h2>
        <form className="paper-form" onSubmit={(event) => void deploy(event)}>
          <label>
            Strategy id
            <input
              value={strategyId}
              onChange={(event) => setStrategyId(event.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <label>
            Version
            <input
              value={version}
              onChange={(event) => setVersion(event.target.value)}
              inputMode="numeric"
              autoComplete="off"
            />
          </label>
          <button type="submit" disabled={busy}>
            Deploy on PAPER
          </button>
        </form>
      </section>
    </div>
  );
}

export function PaperBook({
  snapshot,
  busy = false,
  onUndeploy,
}: {
  snapshot: PaperSnapshot;
  busy?: boolean;
  onUndeploy?: (strategyId: string, version: number) => void;
}) {
  return (
    <>
      <section className="card" aria-label="Running paper strategies">
        <h2>Running strategies</h2>
        {snapshot.strategies.length === 0 ? (
          <p className="empty">No paper strategy is running.</p>
        ) : (
          <div className="paper-scroll">
          <table className="paper-table">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Version</th>
                <th>Status</th>
                <th>Registered</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {snapshot.strategies.map((strategy) => (
                <tr key={`${strategy.strategyId}:${strategy.version}`}>
                  <td>
                    {strategy.name ? <div>{strategy.name}</div> : null}
                    <div className="mono">{strategy.strategyId}</div>
                  </td>
                  <td>{strategy.version}</td>
                  <td>{strategy.status}</td>
                  <td>{strategy.registeredAt}</td>
                  <td>
                    <button
                      type="button"
                      className="secondary"
                      disabled={busy}
                      onClick={() => onUndeploy?.(strategy.strategyId, strategy.version)}
                    >
                      Undeploy {strategy.strategyId} version {strategy.version}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </section>
      <section className="card" aria-label="Paper positions">
        <h2>Positions</h2>
        {snapshot.positions.length === 0 ? (
          <p className="empty">No paper position is stored.</p>
        ) : (
          <div className="paper-scroll">
          <table className="paper-table">
            <thead>
              <tr>
                <th>Asset</th>
                <th>State</th>
                <th>Qty</th>
                <th>Entry</th>
                <th>PAPER P&L</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.positions.map((position) => {
                const pnl = paperPnl(position);
                return (
                  <tr key={`${position.strategyId}:${position.version}:${position.assetId}`}>
                    <td>{position.assetId}</td>
                    <td>{position.state}</td>
                    <td>{position.qty ?? "—"}</td>
                    <td>{position.entry ?? "—"}</td>
                    <td>
                      <div>Stored realized {formatPaperAmount(pnl.realized)}</div>
                      <div>Mark {pnl.markPrice ?? "none"} (last stored close)</div>
                      {pnl.combined !== null && pnl.combinedLabel !== null ? (
                        <div>
                          {pnl.combinedLabel} {formatPaperAmount(pnl.combined)}
                        </div>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        )}
      </section>
      <DriftReport snapshot={snapshot} />
      <ShadowQuotes report={snapshot.shadow} />
      <section className="card" aria-label="Paper event log">
        <h2>Event log</h2>
        {snapshot.eventsHasMore ? <p className="paper-note">Showing the latest events.</p> : null}
        {snapshot.events.length === 0 ? (
          <p className="empty">No paper event is stored.</p>
        ) : (
          <div className="paper-log paper-scroll">
            <table className="paper-table">
              <thead>
                <tr>
                  <th>Open time</th>
                  <th>Kind</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.events.map((event, index) => (
                  <tr key={`${event.strategyId}:${event.version}:${event.openTime}:${event.kind}:${index}`}>
                    <td>{event.openTime}</td>
                    <td>{event.kind}</td>
                    <td className="mono">{event.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

export function mergeEvents(previous: PaperEvent[], incoming: PaperEvent[]): PaperEvent[] {
  const seen = new Set(previous.map((event) => event.id));
  const merged = previous.slice();
  for (const event of incoming) {
    if (seen.has(event.id)) {
      continue;
    }
    seen.add(event.id);
    merged.push(event);
  }
  merged.sort((left, right) => left.id - right.id);
  return merged;
}

export function ShadowQuotes({ report }: { report: ShadowReport }) {
  const rate =
    report.amountMismatchRate === null
      ? "no build attempted"
      : `${report.amountMismatchRate} (${report.amountMismatches} / ${report.buildAttempts})`;
  return (
    <section className="card" aria-label="Shadow quotes">
      <h2>Shadow quotes</h2>
      <p className="paper-note">
        {report.enabled
          ? `Shadow quotes are on. Request slippage ${report.slippageBps ?? "unset"} bps. Interval ${report.minIntervalSeconds ?? "unset"} s. Daily cap ${report.dailyCap ?? "unset"}.`
          : "Shadow quotes are off. SHADOW_QUOTES_ENABLED is not 1."}{" "}
        Quoted output sits next to the paper fill. Network fee, priority fee, and token-account rent
        are not part of strategy P&L. amount_mismatch rate {rate}.
      </p>
      {report.quotes.length === 0 ? (
        <p className="empty">No shadow quote is stored.</p>
      ) : (
        <div className="paper-scroll">
          <table className="paper-table">
            <thead>
              <tr>
                <th>Open time</th>
                <th>Side</th>
                <th>Paper out</th>
                <th>Quoted out</th>
                <th>Impact</th>
                <th>Route</th>
                <th>Network</th>
                <th>Priority</th>
                <th>Rent</th>
                <th>Latency</th>
                <th>Build</th>
              </tr>
            </thead>
            <tbody>
              {report.quotes.map((quote) => (
                <tr key={`${quote.strategyId}:${quote.version}:${quote.openTime}:${quote.side}`}>
                  <td>{quote.openTime}</td>
                  <td>{quote.side}</td>
                  <td>{quote.paperOut}</td>
                  <td>{quote.quotedOut ?? "—"}</td>
                  <td>{quote.priceImpact ?? "—"}</td>
                  <td>{quote.routeLabel ?? "—"}</td>
                  <td>{fee(quote.networkFeeLamports)}</td>
                  <td>{fee(quote.priorityFeeLamports)}</td>
                  <td>{fee(quote.rentLamports)}</td>
                  <td>{quote.latencyMs === null ? "—" : `${quote.latencyMs} ms`}</td>
                  <td>{quote.buildStatus}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function fee(value: number | null): string {
  return value === null ? "—" : `${value} lamports`;
}

function DriftReport({ snapshot }: { snapshot: PaperSnapshot }) {
  return (
    <section className="card" aria-label="Backtest versus paper drift">
      <h2>Backtest versus paper</h2>
      <p className="paper-note">
        Flag when a side or time differs, or when the price delta or the realized P&L delta is
        greater than {snapshot.driftThresholdName} ({formatPaperAmount(snapshot.driftThreshold)}).
        The comparison uses the pinned version and that version&apos;s commission and slippage. It
        is not stored.
      </p>
      {snapshot.drift.length === 0 ? (
        <p className="empty">No running strategy has a drift report.</p>
      ) : (
        <div className="paper-scroll">
          <table className="paper-table">
            <thead>
              <tr>
                <th>Strategy</th>
                <th>Status</th>
                <th>Fills</th>
                <th>Side/time</th>
                <th>Price delta</th>
                <th>Realized P&L delta</th>
                <th>Flag</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.drift.map((row) => (
                <tr key={`${row.strategyId}:${row.version}`}>
                  <td className="mono">
                    <div>
                      {row.strategyId} v{row.version}
                    </div>
                    {row.costs ? (
                      <div>
                        commission {row.costs.commissionBps} bps, slippage {row.costs.slippageBps} bps
                      </div>
                    ) : null}
                  </td>
                  <td>{driftStatus(row)}</td>
                  <td>{fills(row)}</td>
                  <td>{row.sideTimeMismatches === null ? "—" : row.sideTimeMismatches}</td>
                  <td>{row.priceDelta === null ? "—" : formatPaperAmount(row.priceDelta)}</td>
                  <td>{realizedDelta(row)}</td>
                  <td>{row.flagged ? <span className="paper-flagged">flagged</span> : "clear"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function driftStatus(row: PaperDrift): string {
  if (row.status === "insufficient_data") {
    return `insufficient data (${driftReason(row.reason)})`;
  }
  if (row.status === "drift") {
    return "drift";
  }
  return "match";
}

function driftReason(reason: string | null): string {
  if (reason === "no_window") {
    return "no comparison window";
  }
  if (reason === "prior_book") {
    return "position is from an earlier deployment";
  }
  if (reason === "short_history") {
    return "stored history is shorter than warmup";
  }
  if (reason === "bar_missed") {
    return "BAR_MISSED";
  }
  if (reason === "purged_fills") {
    return "purged fills";
  }
  if (reason === "window_before_retained") {
    return "window starts before retained data";
  }
  if (reason === "pinned_document") {
    return "pinned document is unusable";
  }
  if (reason === "backtest_failed") {
    return "backtest could not be run";
  }
  return reason ?? "missing";
}

function fills(row: PaperDrift): string {
  if (row.fillCount === null) {
    return "—";
  }
  return `${row.fillCount.paper} / ${row.fillCount.backtest}`;
}

function realizedDelta(row: PaperDrift): string {
  if (row.realizedPnl === null) {
    return "—";
  }
  const pnl = row.realizedPnl;
  return `delta ${formatPaperAmount(pnl.delta)} (paper ${formatPaperAmount(pnl.paper)}, backtest ${formatPaperAmount(pnl.backtest)})`;
}
