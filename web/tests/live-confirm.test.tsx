import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LiveConfirm } from "@/components/live-confirm";
import { bytesToBase64 } from "@/lib/live";

const UNSIGNED = bytesToBase64(Uint8Array.from([1, 2, 3, 4]));
const TRADE = {
  pending_id: 7,
  expires_at: 1_700_000_060,
  sent: false,
  transaction_b64: UNSIGNED,
  trade: {
    input_mint: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    output_mint: "So11111111111111111111111111111111111111112",
    in_amount: 2_500_000,
    out_amount: 25_000_000,
    minimum_out_amount: 24_875_000,
    slippage_bps: 50,
    price_impact: "-0.1",
    network_fee_lamports: 5_000,
    priority_fee_lamports: 1_000,
    rent_lamports: 1_000_000,
    venue_fee_amount: 0,
    notional_usd: "2.5",
    limits_code: "ok",
  },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("manual confirm", () => {
  it("shows the decoded trade and signs the same bytes in the wallet", async () => {
    const seen: Uint8Array[] = [];
    const bodies: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        bodies.push(JSON.parse(String(init?.body)));
        if (String(input).endsWith("/api/live/prepare")) {
          return new Response(JSON.stringify(TRADE), { status: 200 });
        }
        return new Response(JSON.stringify({ signature: "mock-signature", confirmed: true, sent: true }), {
          status: 200,
        });
      }),
    );
    render(<LiveConfirm />);
    window.dispatchEvent(
      new CustomEvent("wallet-standard:register-wallet", {
        detail: ({ register }: { register: (wallet: object) => void }) => {
          register({
            name: "Test",
            accounts: [{ address: "wallet-address", publicKey: new Uint8Array(32) }],
            features: {
              "solana:signTransaction": {
                signTransaction: async (input: { transaction: Uint8Array }) => {
                  seen.push(input.transaction);
                  const signed = new Uint8Array(input.transaction);
                  signed[0] = 9;
                  return [{ signedTransaction: signed }];
                },
              },
            },
          });
        },
      }),
    );
    expect(await screen.findByText(/Wallet wallet-address/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Prepare unsigned" }));
    expect(await screen.findByText("24,875,000".replaceAll(",", ""))).toBeTruthy();
    expect(screen.getByText("5000")).toBeTruthy();
    expect(screen.getByText("1000")).toBeTruthy();
    expect(screen.getByText("1000000")).toBeTruthy();
    expect(screen.getByText("2.5")).toBeTruthy();
    expect(screen.queryByText(UNSIGNED)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Sign and submit" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("confirmed mock-signature"));
    expect(seen).toHaveLength(1);
    expect(Array.from(seen[0] ?? [])).toEqual([1, 2, 3, 4]);
    expect(bodies[1]).toMatchObject({ pending_id: 7 });
    expect(JSON.stringify(bodies[1])).not.toContain(UNSIGNED);
  });
});
