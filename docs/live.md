# Live trading

Phase 4 places real Solana spot swaps from the same runner that paper already uses. This document is the design and the threat model for that phase. It specifies the layers, the custody choices, the order lifecycle, the limits, and which later task implements each control. It adds no runtime, no dependency, and no configuration.

The process today still only simulates. Paper fills, the live bar feed, and the backtester stay as they are. `docs/runner.md` is the decision function. `docs/paper.md` is the simulated book. `docs/live-feed.md` is the closed-bar feed.

## Scope

One person, one machine, long-only spot. The strategy can buy an asset and later sell that long. It does not borrow, lend, short, or trade perps, and it does not use leverage.

Phase 4 is personal use. There is no counsel or legal review gate on these tasks. Platform fees charged by this app, copy trading, and payments are later phases and are outside this design. A venue request in this phase does not set a referral fee.

The canary, written up in P4-10, uses the micro limits in [Limits and the kill switch](#limits-and-the-kill-switch). Those defaults are the canary sizes.

`app.runner.step` stays the decision function for backtest, paper, and live. Live adds layers after the decision. The frozen backtest engine and the runner core are unchanged by this design.

## Architecture

Four layers, in this order. A later layer cannot change the runner's decision, and it cannot skip the risk engine.

```
closed bar
    |
    v
runner core          app.runner.step          decision only
    |
    v
risk engine          P4-04                    authorize or reject
    |
    v
ExecutionVenue       P4-02                    Jupiter Swap API
    |
    v
signer               P4-05 or P4-06           manual, or local auto
    |
    v
confirm / reconcile  P4-07                    chain balances are the fill
```

The runner is pure: bars and state in, a decision and a new state out. It does not read a clock, a key, or the network (`docs/runner.md`). The risk engine is a pure function of its inputs (the decision, the quote, the limit config, the day's totals, the kill flag, and the timestamps the tick passes in). The same inputs always accept or reject the same way. The tick and the routes call that function. They do not keep a second copy of the rules.

`ExecutionVenue` is the only surface that talks to a swap venue. The first implementation is the Jupiter Swap API. Another venue would be a different implementation of the same interface. The runner never mentions Jupiter.

The lifecycle calls the venue twice around the risk engine, because the check needs a quote and the signer needs a transaction built from a quote the check has accepted:

1. The venue returns a quote. No transaction is signed.
2. The risk engine accepts or rejects that quote.
3. Only an accepted quote is built into an unsigned transaction.
4. The signer signs that transaction.
5. The venue or the local RPC submits it.
6. Confirm and reconcile read the chain.

Shadow mode (P4-03) records the quote next to the paper fill. When build is enabled it also requests an unsigned transaction so the amount mismatch between the quote and the build can be counted. It does not sign or send, and it does not keep the transaction bytes.

## Custody

Two ways to sign are in scope. Both stay on the one local user. Both send every live order through the P4-04 risk engine.

### Option A: manual confirm (P4-05)

The server builds an unsigned swap and shows a pending trade: the quote, the price impact, the limit check, and the fee lines from [Fees, rent, and strategy P&L](#fees-rent-and-strategy-pl). The user signs in their own browser wallet. The server never receives a private key, a seed, or a keypair.

The server's job after the signature is to submit the signed transaction and confirm it on-chain. A pending trade expires at the end of one bar, and sooner if the venue quote expires first. An expired pending trade is discarded. Its bytes cannot be submitted later.

This phase has one local user. Option A is that user's manual path.

### Option B: own-wallet auto signer (P4-06)

A separate trading wallet, automatic, internal only. CoS, the operator of the machine that runs this process, generates the keypair on that box as part of P4-06. The user funds that wallet. The bot may sign on its own, and only inside the limits below.

The secret is loaded from the OS keychain or from an encrypted file outside the repo, pointed at by an environment variable. Startup of this signer requires all of the following:

- `LIVE_TRADING_ENABLED=1`
- `LIVE_SIGNER=local`
- the marked wallet value is at or under the wallet ceiling
- the process is bound to loopback
- the local auth token is configured

Any other combination refuses to start. The signer does not sign and does not send.

Option B is not offered to anyone else. There is no second user, no signup, and no path that signs for another person.

### Out of scope, and rejected

Delegated or session keys (a policy-limited signer, an MPC policy, a multisig spending limit, or an SPL delegate) are Option C. They are out of scope for Phase 4.

Option D is off the table. The server does not generate, store, or sign with keys for other people. There is no hosted-wallet path, no per-user key table, and no code that accepts a key from a caller.

## Order lifecycle

A live order exists only for a decision `app.runner.step` emitted on a real closed bar. A synthetic bar, a hole, and a bar the feed has not stored yet produce no live order. At most one live submission is in flight for an asset. A second action on that asset while one is in flight is rejected, the same way the paper broker rejects a second open order (`docs/paper.md`).

The steps, in order:

1. **Decide.** The feed stores the closed bar, then the runner steps it. The decision is the one the paper path already knows. Live does not recompute it with a different function.
2. **Quote.** `ExecutionVenue` fetches a quote for that decision's side, mint, and size. The quote carries the expected output, the price impact, the route label, the venue's own expiry, and any venue fee. Shadow mode records this and stops.
3. **Risk check.** The pure risk function runs. A failure rejects the order. The size is not reduced to squeeze under a cap. The quote is not replaced with a worse one.
4. **Build.** The venue builds an unsigned transaction from the accepted quote. The built amounts, mints, minimum output, and taker are checked against the quote that passed. A mismatch is a reject. The transaction is not signed.
5. **Sign.** Option A waits for the browser wallet, until the pending trade expires. Option B signs in-process on the same tick. Both require the kill flag to still be clear at the moment of signing.
6. **Send.** One signed transaction is submitted for that decision. The kill flag is checked again immediately before the bytes leave the process.
7. **Confirm and reconcile.** The fill is the change in the wallet's token balances parsed from the landed transaction. The quote, and any JSON the venue returns, are not the fill. Every tick compares those balances with the book. A mismatch halts new scheduled orders. Kill and halt do not send a reversing swap.

An explicit flatten is a separate operator action. It is documented with the kill switch below. It uses this same lifecycle.

### Timeouts

Each waiting point has a deadline. When the deadline passes, the order stops moving forward.

| Point | Deadline | When it passes |
| --- | --- | --- |
| Quote | The expiry the venue returns with the quote | The quote is discarded. No build. |
| Pending trade (Option A) | One bar after that bar's close, or the venue expiry, whichever is sooner | The unsigned bytes are discarded and cannot be submitted. |
| Sign (Option B) | The same tick that passed the risk check, and before the venue expiry | No signature is produced. |
| Submit | While the venue expiry is still in the future | After expiry the bytes are not resubmitted. |
| Confirm | Until the chain reports the signature, or the venue expiry has passed and one later reconcile tick has run | The decision is `landed`, `failed`, or `unknown`. |

The venue's expiry is part of the quote. Aggregator routes express it as a last-valid block height. RFQ routes express it as a timestamp. A quote with no expiry is malformed and is rejected. This design does not hardcode a blockhash lifetime. P4-02 reads the field the response actually carries. See [Open points](#open-points).

Option A's one-bar deadline is the strategy's bar size, measured from the bar close on the same clock as the live feed. It is not the paper broker's 300-second simulated-order TTL.

### Retries

Retries are bounded, and they are not all the same kind.

- **Before any transaction exists.** A quote or build request that times out, returns HTTP 429, or returns HTTP 5xx may be retried. The cap and the backoff follow the live feed: at most five attempts, a bounded sleep, and a numeric `Retry-After` when it is longer (`docs/live-feed.md`). The response body is not stored and not logged. When the attempts run out, the decision is rejected for this tick.
- **The same signed bytes.** While the venue expiry is still in the future and the chain has not reported a terminal status, the same signed transaction may be submitted again. On Solana the signature identifies the transaction, so a replay of those bytes cannot apply a second time. This is a resubmit, not a new order.
- **A new transaction.** Building a second transaction for the same decision is allowed only after all of the following are true: the previous signature is absent on-chain, the previous expiry height or timestamp has passed, and reconcile shows the wallet balances unchanged. That replacement is capped at one per decision. If the replacement also ends `unknown` or `failed`, the decision stops. No third build.

An outcome that is not yet known is not retried with new bytes. HTTP success from the venue is not a fill, and HTTP failure after the bytes were handed off is not proof the transaction was dropped.

### Idempotency

Each decision has one idempotency key:

`user_id`, `strategy_id`, `version`, `asset_id`, `rule_id`, bar `open_time`, `side`, action kind

The action kind is the one the runner emitted (`entry`, `exit`, `oco_stop`, `oco_take_profit`). One key has at most one built transaction, plus the single replacement above. The key's row is restart-durable, in the same sense as the paper cursor: a crash after send must still see the key on the next boot. The row holds the idempotency key, the status, the venue request id, the signature once one exists, the mints, the amounts, and timestamps. It does not hold a private key, a seed, an API key, or the transaction bytes.

On startup, any key that is `submitted` or `unknown` is reconciled before a new live order is allowed. A missing row plus a wallet that moved is a mismatch, and the process halts.

## Limits and the kill switch

These are config defaults. P4-04 implements them as the pure checks. P4-09 shows how much of each has been used today. An order that fails a check is rejected. It is not resized.

| Limit | Default | What happens |
| --- | --- | --- |
| Per-trade notional | $2.50 | The order is rejected. |
| Notional per day | $10 | Scheduled live orders halt for the rest of that day. |
| Realized loss per day | $5 | Scheduled live orders halt for the rest of that day. |
| Open live positions | 3 | A new entry is rejected at the cap. |
| Allowlist | SOL, JUP, JTO, BONK, WIF | A buy of any other asset is rejected. |
| Wallet ceiling | $25 | Auto-signer startup refuses when the marked value is above $25, or when the mark cannot be computed. |
| `LIVE_TRADING_ENABLED` | off | The only on value is `1`. Unset, blank, or any other value is off. |

The allowlist symbols are the OHLCV registry majors (`docs/ohlcv.md`, `docs/live-feed.md`). USDC is a sell-route quote target, not an entry on the allowlist. A position is one held asset on the trading wallet. A second strategy cannot open an asset that is already open.

Dollar limits use one USD mark, chosen and documented in P4-04, for the per-trade notional, the daily notional, the daily loss, and the wallet ceiling. A missing mark rejects the order and refuses signer startup. The day key is the server's UTC date (`YYYY-MM-DD`). A request body does not choose it. The stored day key moves forward only. A clock step backward does not reset the day's totals or clear a halt. The row is in the bar-store sqlite file: notional, realized loss, the fee columns, and the halt flag. It has no price and no payload, and the 24-hour purge does not delete it. A restart reads that row.

The $5 loss figure is strategy realized loss from on-chain in and out amounts. Network fees, priority fees, and token-account rent are not added into it. They are the separate lines below. P4-04 also halts the day when those three lines together pass a separate daily fee cap (20,000,000 lamports). One transaction is rejected when its own three lines pass 5,000,000 lamports. A venue platform fee stays on its own line and is not part of either cap.

Further gates. The numbers live in `app.risk.limits` and are the checks P4-04 runs:

- Maximum slippage is 100 bps. Live `/order` and `/build` send `slippageBps` 50, the risk param, and never omit it. Above the cap, the order is rejected. A minimum-out that implies more than 100 bps is rejected too.
- Maximum price impact is 1 on the quote's `priceImpact` scale. Above it, the order is rejected.
- Minimum pool liquidity is $50,000. Minimum pool age is 86,400 seconds. A missing reading rejects the buy.
- Freeze authority and mint authority. A buy is rejected when either authority is still set, and when the mint account cannot be read.
- A sell-route check. Before a buy, a quote that sells the would-be output back to SOL or USDC must exist and must itself pass the impact cap. The check is a quote, not a promise that the route will still exist later.
- A stale-data check. No order when the latest closed bar is older than 2 bars, or when the bar and the quote disagree by more than 1 percent.
- The allowlist does not skip the authority, liquidity, age, or sell-route checks. A major that fails one of them is not buyable until that check's rule is changed in P4-04.

The wallet ceiling is checked again at the start of each tick. Above the ceiling, new buys halt. A scheduled sell of an already open long still uses the normal lifecycle, so a position can be closed and the wallet can shrink. That running behavior is this design's reading of a rule whose required statement is the startup refusal. P4-04 owns the shared check. P4-06 refuses to start the auto signer above the ceiling.

### Kill switch

`LIVE_TRADING_ENABLED` defaults off. With it off, no pending trade is created and nothing is signed or sent.

P4-04 also has a runtime halt:

- `POST /live/kill` halts within one tick and cancels pending unsigned trades.
- A file (`LIVE_KILL_FILE`) is read at the top of every tick. The only clear contents are `0` or `0\n`. Missing, unreadable, or any other contents halt. `LIVE_KILL=1` halts too. The read does not use the API, the database, or the control token, so a later signer can use the same function. `POST /live/kill` writes `1` and cancels pending unsigned trades.
- A dead-man switch: if the scheduler gap is longer than 2 intervals, it halts and writes the kill file. It does not catch up by emitting an order per skipped bar. A live order, when the gate is clear, is considered only for the newest bar.
- A live order is considered only for the newest closed bar that is still inside the quote and pending deadlines. Older missed bars are logged and skipped.

Halt and kill cancel unsigned work and stop the scheduler from sending. A transaction that has already been submitted is left to confirm and reconcile. It may still land. The book records whatever the chain shows.

Flatten-all is a separate, explicit operator action. The kill path does not call it. A daily halt does not call it. The operator can still request it while halted. That request runs the full lifecycle (quote, risk checks on slippage, impact, and the built transaction, then sign, send, and reconcile). It is a sell of an open long. It is not a new entry, and it is not sent by the scheduler on its own.

### Fees, rent, and strategy P&L

Reports show three cost lines apart from strategy P&L:

- the network fee
- the priority fee
- token-account rent

Strategy P&L is the trade's in and out amounts at the fill the chain reports. Those three costs are not folded into that figure. On a $2.50 trade they are large relative to the trade, which is why they have their own lines.

P4-04 accounts for them as separate values. P4-07 reads the paid amounts from the landed transaction. P4-08 stores them with the durable fill. P4-09 displays them. A venue platform fee, if the quote includes one, is a fourth separate line, not part of strategy P&L and not one of the three costs above. This phase does not add a referral fee on top.

## Key handling

CoS generates the trading keypair on the CoS box during P4-06. That is the only place the keypair is created. The user funds the wallet. The secret lives only in the OS keychain or in an encrypted file outside the repo, on that same box, and it is loaded only by the localhost-bound runtime.

The secret never goes into the repo, a log line, sqlite, an error message, or CI. A signature, a public address, a mint, and an amount may be stored. The secret, the seed, the keypair bytes, a signed transaction blob, a venue API key, and the local auth token may not.

The public address is configured as data the process is allowed to hold. The doc and the repo use a placeholder such as `<PUBKEY>` if an example is required. They do not contain a real trading address.

The current auth seam returns a fixed local user and ignores any caller-supplied identity (`docs/design-notes.md`). That seam is not the authorization to sign. P4-06 adds a local auth token, present only on the box, and requires it on any route that can sign, kill, or flatten. A request without the token is rejected. The token is not written to the repo, logs, sqlite, error messages, or CI.

If the Jupiter client needs an API key, that value is an environment variable on the CoS box and follows the same rules. CI does not have it. Tests do not need it.

## Threat model

Each row is a way live trading can lose money or leak a secret, the control this design requires, and the task that implements the control. P4-01 records the map. It does not implement the controls.

| Threat | What goes wrong | Control | Task |
| --- | --- | --- | --- |
| Key theft via the repo | The repo is public, so a committed secret is a leaked secret. | The keypair is generated on the CoS box and never committed. gitleaks and trufflehog are required on every PR diff. | P4-06. Scans are standing from P4-01 on. |
| Key leak via logs | A log line, including a debug dump, keeps the secret or a signed blob. | Logs carry an event kind, ids, status, an exception type, and a short message. They omit the secret, the seed, signed bytes, raw venue bodies, the API key, and the auth token. | P4-06, P4-07 |
| Key leak via sqlite | A local database file is copied and contains the secret. | sqlite holds the book, quotes, the idempotency row, and later the own-transaction ledger. It never holds secret bytes, signed blobs, or the auth token. | P4-06, P4-08 |
| Key leak via error messages | An exception string echoes a header, a body, or a path that contains the secret. | Errors are a type plus a short message. The venue body and the request URL's secret parameters are not included. The same rule the live feed already uses for provider errors. | P4-02, P4-06 |
| Key leak via CI | A workflow secret or a test fixture can sign, or a log uploads the secret. | CI has no trading key and no venue API key. Tests use a mock venue, or a local validator when a later task needs one. `LIVE_TRADING_ENABLED` stays unset. | Standing. P4-02 supplies the mock. |
| Stuck or dropped transaction | The submit call returns and the transaction never lands, or it lands after the client gave up. The book is then wrong, or a retry sends a second swap. | One idempotency key per decision. Resubmit only the same signed bytes. A new build requires proof the previous signature is absent, the expiry has passed, and the balances are unchanged, and it is capped at one. `unknown` does not build again. | P4-07 |
| Partial fills | The wallet moves by a different amount than the order. | The fill is the on-chain balance change. A Jupiter swap is treated as atomic: the signed transaction lands entirely or not at all. Output may be lower than quoted, down to the minimum encoded in the transaction. Any other balance change, including a partial spend of the input, is a mismatch and halts. Dust left after a sell keeps the position open and counting toward the cap of 3. | P4-04, P4-07 |
| Slippage | The fill price is worse than the decision assumed. | The risk engine rejects when slippage or price impact is above its cap. It does not resize. The built transaction's minimum output must match the approved quote. A looser minimum is a reject before sign. | P4-04, P4-02 |
| RPC down | The process cannot submit, or cannot tell whether a submit landed. | No build and no send when the RPC or the venue cannot be reached. After a send, a failed confirm read leaves the key `unknown` and stops new orders until a reconcile read succeeds. A failed read is `unknown`, not a fabricated mismatch. | P4-04, P4-07 |
| Stale bars | An order fires on an old close, or on a bar that does not match the quote. | No order when the latest closed bar is older than 2 bars, or when the bar and the quote disagree by more than 1 percent. Missed bars are skipped, not traded as a burst. The feed's existing close grace still applies (`docs/live-feed.md`). | P4-04. The feed itself stays off until the ROZ-107 C-R1 gate below. |
| Rugged token | The pool's price collapses after a buy. | Entries start on the SOL and majors allowlist, and still need the liquidity and pool-age floors. The daily loss cap and the kill switch stop new scheduled buys. Flatten stays a separate explicit action, so a crash does not trigger an automatic sell. | P4-04 |
| Frozen token | A freeze authority stops a later sell. | A buy is rejected when the mint has a freeze authority or a mint authority set, and when that account cannot be read. | P4-04 |
| Honeypot token | A buy succeeds and no route can sell. | Before a buy, a quote back to SOL or USDC for the would-be output must exist and pass the impact cap. A missing or failed sell quote rejects the buy. | P4-04 |
| Jupiter API errors | HTTP errors, empty transactions, or an error code come back instead of a swap. | Those responses do not sign and do not send. Retries happen only before a transaction exists, and they are capped. The logged detail is the router and the error code, not the body. | P4-02, P4-03 |
| Hostile or malformed venue response | The body quotes one pair and the transaction moves a different mint, a larger amount, or value to an unexpected account. | The quote is parsed strictly. Transaction bytes that arrive before the risk check are not signed. After build, the decoded transaction must match the approved mints, amounts, minimum output, and taker. The venue's execute JSON is untrusted: a success flag does not write a fill, and a failure flag does not erase a signature the chain shows. The chain wins. | P4-02, P4-04, P4-07 |
| Runaway loops | A bug retries without a bound, or catch-up emits one order per missed bar. | Pre-sign HTTP retries are capped. Post-sign replacement is capped at one. One in-flight order per asset. Missed ticks halt rather than catch up. Daily notional and daily loss caps stop a loop that is otherwise "working". | P4-04, P4-07 |
| Clock skew | A fast clock treats a forming bar as closed, or a stepped-back clock resets the daily caps. | The feed's 60-second close grace remains. Daily totals use a day key that only moves forward. Pending expiry and venue expiry are both enforced, so a slow local clock cannot keep a dead quote alive. Confirm uses the chain's view of the signature. | P4-04, P4-07 |
| Localhost-binding breach | A process off the box, or another local process, reaches a route that can sign. | The signing process listens on loopback only. Startup of the auto signer refuses any other bind. Sign, kill, and flatten require the local auth token. The fixed `local-user` seam is not that token. Option A's pending trade is created only by a local risk-approved decision, and only that pending trade can be submitted. | P4-05, P4-06 |

## Retention and logging

The 24-hour purge carries over. GeckoTerminal and Birdeye bars, raw provider payloads, derived marks, recorded decisions, and live-feed error rows still expire at 86400 seconds (`docs/live-feed.md`). P4-03 quote records expire on that same clock. A quote is not a bar, and it is not kept longer for that reason.

Option 1 carries over from `docs/paper.md`. The wallet row and the open-position row have no `stored_at`, and the purge does not delete them. When the long goes flat, the price fields are cleared and the row stays so lifetime realized is not lost. The book is not rebuilt from fills, decisions, or bars that have aged out. On the live wallet the open-position row is one per held asset, with the strategy id that opened it stored on the row. The chain token account for that mint is the reconcile source. The paper books stay per strategy version, as they are now.

The idempotency row is operational state, like the paper cursor. It is not part of the 24-hour market-data purge, because a purge during an in-flight send would forget the signature. P4-07 defines when a terminal row can be dropped. Until the durable ledger exists, that row is the local record of the signature.

Durable records of the user's own transactions are decided in P4-08. The intended contents are the transaction signature, the mints, the amounts in and out, the fee lines, and the timestamp, taken from those own on-chain transactions. They are local to the runtime and are not committed. This document does not define that schema. Market-data series stay on the 24-hour purge either way.

What is logged, and for how long:

| Record | Kept | Lifetime |
| --- | --- | --- |
| Event kind, ids, status, exception type, short message | Yes | 24 hours, with the existing event purge |
| Quote output, impact, route label | Yes | 24 hours (P4-03) |
| Public signature, mints, amounts, fee lines | Yes, on the idempotency row, then on the P4-08 ledger | Operational until P4-07 expires the row. The ledger lifetime is P4-08's decision. |
| Venue response body, request URL, signed bytes | No | Not stored |
| Secret, seed, API key, auth token | No | Not stored |

## Gates

These are merge and runtime gates. A later task does not skip one because the code is ready.

- `LIVE_TRADING_ENABLED` defaults off. The only enabling value is `1`.
- P4-04 (the risk engine and the kill switch) merges before any signer PR. P4-05 and P4-06 are blocked on that merge.
- CI never holds a trading key or a venue API key, and it never sends a transaction. Tests use a mock venue, or a local validator when a task needs a chain. The workflow does not set `LIVE_TRADING_ENABLED`.
- gitleaks and trufflehog are required on every PR diff. They are a standing check on the diff. They are not a substitute for the logging and storage rules above.
- ROZ-107 C-R1 is fixed in P4-03, before `LIVE_FEED_ENABLED` is turned on. Drift and the broker share the origin recorded at the broker's first tick. A later backfill of older bars does not move that origin, so it does not flag a false drift. `LIVE_FEED_ENABLED` stays default-off in this phase. Live orders depend on that feed, so the flag stays off until an operator turns it on.
- The runtime that can sign is bound to loopback. The auto signer refuses to start otherwise.
- The canary uses the micro limits in this document. Turning the canary from shadow, to manual confirm, to auto is an operator decision after P4-10's runbook, not a side effect of merging these tasks.

## Task map

Tasks land in this order: P4-01, P4-02, P4-03, P4-04, then P4-05 and P4-06 in either order, then P4-07, P4-08, P4-09. P4-10 can be written once this design is in place. Running the canary waits until P4-01 through P4-09 have merged and the operator says to run it.

| Task | Issue | What it ships | Controls |
| --- | --- | --- | --- |
| P4-01 | ROZ-108 | This design and threat model. Docs only. | Records custody, the lifecycle, the limits, the gates, and the threat-to-task map. No signer, no venue client, no order path. |
| P4-02 | ROZ-109 | The `ExecutionVenue` interface and the Jupiter Swap API implementation. | The only venue surface. Strict parse of quotes and built transactions. No sign and no send. CI uses a mock and has no key. |
| P4-03 | ROZ-110 | Quote-only shadow mode beside the paper fill. | Real quotes through the interface, no sign and no send. Bounded venue retries. Quote rows purge at 24 hours. Fixes ROZ-107 C-R1 and leaves `LIVE_FEED_ENABLED` default-off. |
| P4-04 | ROZ-111 | The risk engine and the kill switch. No signer. | The limit table, slippage and impact caps, token gates, the stale-data gate, the wallet-ceiling check, `LIVE_TRADING_ENABLED` defaulting off, `POST /live/kill`, the dead-man halt, and fee/rent amounts kept separate from strategy P&L. Merge gate for both signers. |
| P4-05 | ROZ-114 | Option A, the manual-confirm signer. | The server builds an unsigned transaction and confirms on-chain. It never sees a key. Pending trades expire after one bar. The risk engine still runs. Blocked on P4-04. |
| P4-06 | ROZ-112 | Option B, the own-wallet auto signer. Internal only. | CoS generates the keypair on the CoS box. Loopback bind, local auth token, `LIVE_SIGNER=local`, and the startup ceiling refusal. The secret stays out of the repo, logs, sqlite, errors, and CI. Blocked on P4-04. |
| P4-07 | ROZ-113 | Send, confirm, and reconcile. | Priority fee and a configurable compute budget when we land the transaction ourselves. Idempotency, bounded retries, stuck and unknown transactions, chain balance changes as the fill, halt on mismatch, no auto-flatten. |
| P4-08 | ROZ-115 | The durable ledger of the user's own transactions. | Signature, mints, amounts, fee lines, and timestamp from those own transactions. Fee lines stored separately. Bars and marks still purge at 24 hours. Option 1 kept for live positions. |
| P4-09 | ROZ-116 | Live dashboard and alerts. | Live next to paper, limits used today, the kill control, last reconcile status, alerts on halt, reject, or mismatch. Fee and rent lines shown apart from strategy P&L. The kill control does not flatten. |
| P4-10 | ROZ-117 | The canary runbook and checklist. | Shadow, then manual confirm, then auto, all inside the micro limits. Requires the C-R1 gate, default-off live trading, a loopback runtime, kill and dead-man rehearsal, and a reminder that flatten is never automatic. The runbook does not enable trading. |

## Open points

P4-02 re-reads the Jupiter docs and pins the client. The points below are assumptions this design is making so that later task can confirm or correct them. None of them is a default limit.

- **Venue shape.** The public Jupiter Swap API docs, as read on 2026-11-25, describe a v2 meta-aggregator (`/order` then `/execute`) and a router path (`/build`, then the caller's own RPC or the venue's submit). `/order` returns a quote and, when a taker is set, an unsigned transaction in the same response. Landing via `/execute` means Jupiter submits. This design still requires a quote step, a risk check, and a separate build step. P4-02 may implement the quote as an order request that does not yield a signable transaction, and the build as a later request. If one response contains both, the transaction bytes are not signed until the risk check passes, and they are rejected when they disagree with the checked quote. Endpoint names and error numbers will move. The interface in this document is the stable surface.
- **Atomic swaps and "partial" results.** A landed Solana transaction applies entirely or not at all. The execute response fields that split a venue fee from the swap amounts are a fee split, not a partial fill of the input. This design assumes an exact-in swap: the input is spent in full or the transaction fails, and slippage only reduces the output down to the encoded minimum. P4-02 confirms that an RFQ route cannot settle a fraction of the order in a second transaction this process did not sign. If the chain ever shows a partial input spend, P4-07 halts.
- **Expiry fields.** Aggregator quotes expose a last-valid block height. RFQ quotes expose a timestamp, and that window is shorter. Both are read from the response. A missing expiry is a reject.
- **Venue fee.** The quote can include a Jupiter platform fee even when this phase sets no referral fee. That fee is its own report line. The bps figure is whatever the response says. It is not a config default of this app.
- **Sponsored or gasless transactions.** A small SOL balance may cause the venue to return a transaction with an extra payer. P4-02 rejects a built transaction whose value movement is anything other than the approved swap plus the expected network fee, priority fee, and token-account rent.
- **Extra signature on RFQ.** Some routes require this wallet to sign once, and a market maker signature is added when the venue lands the transaction. This process signs only with the local trading key or the user's browser wallet. It does not hold a second key.
- **USD mark.** The caller supplies one USD mark for the $2.50, $10, $5, and $25 checks. This package does not fetch a price. A missing mark rejects the order and refuses startup.
- **Loss cap versus fees.** The $5 daily realized-loss halt uses strategy P&L and excludes network fees, priority fees, and rent. Those three lines have their own per-transaction cap and their own daily cap, and the daily cap halts the day.
- **Ceiling while already running.** Startup above $25 refuses. A later buy above the ceiling is also rejected. A scheduled sell of an open long still passes that check so the wallet can shrink. Flatten is explicit and is not started by the kill switch.
- **Numbers set in P4-04.** Price impact cap 1, pool liquidity $50,000, pool age 86,400 seconds, stale bars 2, divergence 1 percent, dead-man 2 intervals, per-transaction fee 5,000,000 lamports, daily fee cap 20,000,000 lamports, compute-unit limit 1,400,000, compute-unit price 1,000,000 micro-lamports, priority product 100,000 lamports. P4-03 still sends `slippageBps` on shadow `/order` and `/build` from `SHADOW_SLIPPAGE_BPS` (default 50). Live order and build send the risk param, also 50, and reject above 100 or when minimum-out implies more than that.
- **Authorities on the majors.** This document does not assert that SOL, JUP, JTO, BONK, or WIF currently pass the freeze and mint checks. The checks run. A major that fails is not bought.
- **API key.** Whether the chosen Jupiter path still answers with no API key is a P4-02 fact. If a key is required, it stays on the box, as [Key handling](#key-handling) says.
- **Local auth token.** The token does not exist in the current process. P4-06 introduces it. This document does not define its format, only where it lives and which routes require it.
- **Solana replay.** The retry rule assumes one signature identifies one transaction and a replay of those same bytes cannot settle twice. A new signature is a new transaction and is the case the idempotency key forbids, except for the single replacement after proof of a drop.
