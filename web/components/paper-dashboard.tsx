"use client";

import { useEffect, useState, type FormEvent } from "react";

import {
  errorMessage,
  paperDeployPath,
  paperPnl,
  readSnapshot,
  type PaperSnapshot,
} from "@/lib/paper";

const EMPTY: PaperSnapshot = {
  mode: "paper",
  simulated: true,
  strategies: [],
  positions: [],
  events: [],
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
    const pull = async () => {
      try {
        const response = await fetch("/api/paper", {
          cache: "no-store",
          headers: { accept: "application/json" },
        });
        const body: unknown = await response.json();
        if (cancelled) {
          return;
        }
        if (!response.ok) {
          setError(errorMessage(body, "the paper API is unavailable"));
        } else {
          setSnapshot(readSnapshot(body));
          setError(null);
        }
      } catch {
        if (!cancelled) {
          setError("the paper API is unavailable");
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
      <PaperBook snapshot={snapshot} busy={busy} onUndeploy={(id, number) => void undeploy(id, number)} />
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
                      <div>Stored realized {pnl.realized}</div>
                      <div>Mark {pnl.markPrice ?? "none"} (last stored close)</div>
                      {pnl.combined !== null && pnl.combinedLabel !== null ? (
                        <div>
                          {pnl.combinedLabel} {pnl.combined}
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
      <section className="card" aria-label="Paper event log">
        <h2>Event log</h2>
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
