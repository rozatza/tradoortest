/**
 * Wallet Standard signer. The page never sees a secret key. It hands the
 * unsigned transaction bytes to `solana:signTransaction` and reads the
 * signed bytes back.
 */

const REGISTER = "wallet-standard:register-wallet";
const READY = "wallet-standard:app-ready";
const FEATURE = "solana:signTransaction";

export type WalletAccount = {
  address: string;
  publicKey: Uint8Array;
};

export type StandardWallet = {
  name: string;
  accounts: readonly WalletAccount[];
  features: Record<string, { signTransaction?: SignTransaction } | undefined>;
};

type SignInput = { account: WalletAccount; transaction: Uint8Array };
type SignOutput = { signedTransaction: Uint8Array };
type SignTransaction = (input: SignInput) => Promise<readonly SignOutput[]>;
type RegisterApi = { register: (wallet: StandardWallet) => void };

export function watchWallets(onWallet: (wallet: StandardWallet) => void): () => void {
  const seen = new Set<StandardWallet>();
  const add = (wallet: StandardWallet) => {
    if (seen.has(wallet) || wallet.accounts.length === 0) {
      return;
    }
    if (typeof wallet.features[FEATURE]?.signTransaction !== "function") {
      return;
    }
    seen.add(wallet);
    onWallet(wallet);
  };
  const listener = (event: Event) => {
    if (!(event instanceof CustomEvent)) {
      return;
    }
    const detail: unknown = event.detail;
    if (typeof detail !== "function") {
      return;
    }
    (detail as (api: RegisterApi) => void)({ register: add });
  };
  window.addEventListener(REGISTER, listener);
  window.dispatchEvent(new Event(READY));
  return () => window.removeEventListener(REGISTER, listener);
}

export async function signRaw(wallet: StandardWallet, transaction: Uint8Array): Promise<Uint8Array> {
  const account = wallet.accounts[0];
  const sign = wallet.features[FEATURE]?.signTransaction;
  if (!account || typeof sign !== "function") {
    throw new Error("wallet_unavailable");
  }
  const signed = await sign({ account, transaction });
  const first = signed[0]?.signedTransaction;
  if (!(first instanceof Uint8Array) || first.byteLength === 0) {
    throw new Error("wallet_unavailable");
  }
  return first;
}
