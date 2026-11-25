"use client";

import { useEffect, useState } from "react";

import { base64ToBytes, bytesToBase64, isPreparedTrade, LIVE_MINTS, type PreparedTrade } from "@/lib/live";
import { signRaw, watchWallets, type StandardWallet } from "@/lib/live-wallet";

type Notice = { kind: "error" | "ok"; text: string };

export function LiveConfirm() {
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [action, setAction] = useState("entry");
  const [mint, setMint] = useState<string>(LIVE_MINTS[0].mint);
  const [amount, setAmount] = useState("2500000");
  const [wallet, setWallet] = useState<StandardWallet | null>(null);
  const [pending, setPending] = useState<PreparedTrade | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => watchWallets((next) => setWallet((current) => current ?? next)), []);

  async function prepare() {
    const whole = Number(amount);
    if (!Number.isInteger(whole) || whole <= 0) {
      setNotice({ kind: "error", text: "amount must be a positive whole number of atoms" });
      return;
    }
    setBusy(true);
    setNotice(null);
    setPending(null);
    try {
      const response = await fetch(action === "flatten" ? "/api/live/flatten" : "/api/live/prepare", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          side: action === "flatten" ? "sell" : side,
          action,
          mint,
          amount: whole,
        }),
      });
      const body: unknown = await response.json();
      if (!response.ok || !isPreparedTrade(body)) {
        setNotice({ kind: "error", text: codeOf(body) });
        return;
      }
      setPending(body);
    } catch {
      setNotice({ kind: "error", text: "prepare_failed" });
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!pending || !wallet) {
      setNotice({ kind: "error", text: wallet ? "nothing_to_sign" : "wallet_unavailable" });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const signed = await signRaw(wallet, base64ToBytes(pending.transaction_b64));
      const response = await fetch("/api/live/confirm", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          pending_id: pending.pending_id,
          signed_transaction_b64: bytesToBase64(signed),
        }),
      });
      const body: unknown = await response.json();
      if (!response.ok) {
        setNotice({ kind: "error", text: codeOf(body) });
        setPending(null);
        return;
      }
      const row = body as { signature?: unknown; confirmed?: unknown };
      const signature = typeof row.signature === "string" ? row.signature : "";
      setNotice({
        kind: "ok",
        text: row.confirmed === true ? `confirmed ${signature}` : `submitted ${signature}`,
      });
      setPending(null);
    } catch {
      setNotice({ kind: "error", text: "wallet_unavailable" });
    } finally {
      setBusy(false);
    }
  }

  const trade = pending?.trade;

  return (
    <section className="live" aria-label="Manual confirm">
      <form
        className="live-form"
        onSubmit={(event) => {
          event.preventDefault();
          void prepare();
        }}
      >
        <label>
          Side
          <select value={side} onChange={(event) => setSide(event.target.value === "sell" ? "sell" : "buy")}>
            <option value="buy">buy</option>
            <option value="sell">sell</option>
          </select>
        </label>
        <label>
          Action
          <select value={action} onChange={(event) => setAction(event.target.value)}>
            <option value="entry">entry</option>
            <option value="exit">exit</option>
            <option value="flatten">flatten</option>
          </select>
        </label>
        <label>
          Mint
          <select value={mint} onChange={(event) => setMint(event.target.value)}>
            {LIVE_MINTS.map((item) => (
              <option key={item.mint} value={item.mint}>
                {item.symbol}
              </option>
            ))}
          </select>
        </label>
        <label>
          Amount
          <input value={amount} inputMode="numeric" onChange={(event) => setAmount(event.target.value)} />
        </label>
        <button type="submit" disabled={busy}>
          Prepare unsigned
        </button>
      </form>
      <p>{wallet ? `Wallet ${wallet.accounts[0]?.address ?? ""}` : "No wallet yet. Connect one in this browser."}</p>
      {trade ? (
        <dl className="live-trade">
          <div>
            <dt>Input mint</dt>
            <dd>{trade.input_mint}</dd>
          </div>
          <div>
            <dt>Output mint</dt>
            <dd>{trade.output_mint}</dd>
          </div>
          <div>
            <dt>In amount</dt>
            <dd>{trade.in_amount}</dd>
          </div>
          <div>
            <dt>Out amount</dt>
            <dd>{trade.out_amount}</dd>
          </div>
          <div>
            <dt>Minimum out</dt>
            <dd>{trade.minimum_out_amount}</dd>
          </div>
          <div>
            <dt>Slippage bps</dt>
            <dd>{trade.slippage_bps}</dd>
          </div>
          <div>
            <dt>Price impact</dt>
            <dd>{trade.price_impact}</dd>
          </div>
          <div>
            <dt>Network fee lamports</dt>
            <dd>{trade.network_fee_lamports}</dd>
          </div>
          <div>
            <dt>Priority fee lamports</dt>
            <dd>{trade.priority_fee_lamports}</dd>
          </div>
          <div>
            <dt>Rent lamports</dt>
            <dd>{trade.rent_lamports}</dd>
          </div>
          <div>
            <dt>Notional USD</dt>
            <dd>{trade.notional_usd}</dd>
          </div>
          <div>
            <dt>Expires</dt>
            <dd>{pending?.expires_at}</dd>
          </div>
        </dl>
      ) : (
        <p className="empty">No pending trade. Fees stay on their own lines and are not part of the notional.</p>
      )}
      <button type="button" disabled={busy || !pending} onClick={() => void confirm()}>
        Sign and submit
      </button>
      {notice ? <p role="status">{notice.text}</p> : null}
    </section>
  );
}

function codeOf(body: unknown): string {
  if (body !== null && typeof body === "object") {
    const row = body as { code?: unknown; error?: { code?: unknown } };
    if (typeof row.code === "string") {
      return row.code;
    }
    if (typeof row.error?.code === "string") {
      return row.error.code;
    }
  }
  return "rejected";
}
