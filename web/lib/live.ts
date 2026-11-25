/** Display shape returned by POST /live/prepare. The server derived every number. */

export const LIVE_MINTS = [
  { symbol: "SOL", mint: "So11111111111111111111111111111111111111112" },
  { symbol: "JUP", mint: "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN" },
  { symbol: "JTO", mint: "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL" },
  { symbol: "BONK", mint: "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" },
  { symbol: "WIF", mint: "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm" },
] as const;

export type LiveTrade = {
  input_mint: string;
  output_mint: string;
  in_amount: number;
  out_amount: number;
  minimum_out_amount: number;
  slippage_bps: number;
  price_impact: string;
  network_fee_lamports: number;
  priority_fee_lamports: number;
  rent_lamports: number;
  venue_fee_amount: number;
  notional_usd: string;
  limits_code: string;
};

export type PreparedTrade = {
  pending_id: number;
  expires_at: number;
  trade: LiveTrade;
  transaction_b64: string;
  sent: false;
};

export function isPreparedTrade(value: unknown): value is PreparedTrade {
  if (value === null || typeof value !== "object") {
    return false;
  }
  const row = value as Record<string, unknown>;
  const trade = row.trade;
  if (trade === null || typeof trade !== "object") {
    return false;
  }
  const fields = trade as Record<string, unknown>;
  return (
    typeof row.pending_id === "number" &&
    typeof row.expires_at === "number" &&
    typeof row.transaction_b64 === "string" &&
    row.transaction_b64 !== "" &&
    row.sent === false &&
    typeof fields.input_mint === "string" &&
    typeof fields.output_mint === "string" &&
    typeof fields.in_amount === "number" &&
    typeof fields.out_amount === "number" &&
    typeof fields.minimum_out_amount === "number" &&
    typeof fields.slippage_bps === "number" &&
    typeof fields.price_impact === "string" &&
    typeof fields.network_fee_lamports === "number" &&
    typeof fields.priority_fee_lamports === "number" &&
    typeof fields.rent_lamports === "number" &&
    typeof fields.notional_usd === "string"
  );
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

export function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const out = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    out[index] = binary.charCodeAt(index);
  }
  return out;
}
