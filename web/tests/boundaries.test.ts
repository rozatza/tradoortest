import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const ROOT = path.resolve(import.meta.dirname, "..");
const SOURCE_DIRS = ["app", "components", "lib"];
const FORBIDDEN = [
  "localStorage",
  "sessionStorage",
  "indexedDB",
  "caches",
  "CacheStorage",
  "NEXT_PUBLIC_",
  "privateKey",
  "secretKey",
  "Keypair",
  "mnemonic",
  "createWallet",
  "/tools/",
];

/** The manual-confirm wallet adapter is the only file that may name this feature. */
const SIGN_FEATURE = "signTransaction";
const SIGN_FEATURE_FILE = "lib/live-wallet.ts";

function sourceFiles(dir: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (entry === "node_modules" || entry === ".next") {
      continue;
    }
    const info = statSync(full);
    if (info.isDirectory()) {
      found.push(...sourceFiles(full));
      continue;
    }
    if (entry.endsWith(".ts") || entry.endsWith(".tsx") || entry.endsWith(".css")) {
      found.push(full);
    }
  }
  return found;
}

describe("client boundaries", () => {
  it("keeps secrets, storage, and signing out of the chat UI", () => {
    const files = SOURCE_DIRS.flatMap((dir) => sourceFiles(path.join(ROOT, dir)));
    expect(files.length).toBeGreaterThan(0);
    const hits: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      const relative = path.relative(ROOT, file);
      for (const token of FORBIDDEN) {
        if (text.includes(token)) {
          hits.push(`${relative} contains ${token}`);
        }
      }
      if (text.includes(SIGN_FEATURE) && relative !== SIGN_FEATURE_FILE) {
        hits.push(`${relative} contains ${SIGN_FEATURE}`);
      }
    }
    expect(hits).toEqual([]);
  });
});
