# Paper broker

`service/app/paper` turns a registered strategy into simulated fills. It is one book per `(user, strategy id, version)`: that version's initial capital, cash, and open longs. `user_id` is the auth seam. This process still has one user. There is no key, no signature, no chain, and no order is sent.

The scheduler in `docs/live-feed.md` records decisions with an empty book. This broker does not read those decisions and does not fill them. A bar the scheduler back-decides on the first tick was already closed at registration. Its close time is at or before `registered_at`, and the broker does not fill it.

## What is durable

The wallet row and the position row have no `stored_at`. `_purge` does not delete them.

| Row | Kept | Why |
| --- | --- | --- |
| Wallet | cash, initial capital | A flat book still has a balance after the fill log is gone. Cash is not rebuilt by summing fills. |
| Open position | qty, open cost, entry, exact basis, lifetime realized, this trade's realized, OCO take-profit, stop-loss, anchor, `attached_at` | A long can stay open longer than 24 hours. The next bar needs the entry, the basis (notional plus the buy commission still open), and the live OCO pair. Basis and realized are `numerator/denominator` strings. |
| Paper cursor | last open time | An open time, not a price. The same bar is not filled twice. |

The position row is not a bar series and it does not hold a provider payload. Entry, anchor, and the two OCO levels are the derived numbers the backtester already keeps on the book. They are not a history of closes.

When the long goes flat, the row stays so lifetime realized is not lost, and the price fields are set to null: qty, open cost, entry, basis, this trade's realized, take-profit, stop-loss, anchor, and `attached_at`. The next entry starts a new trade on that row. Lifetime realized is the sum of the closed trades and the open trade's booked sells.

Fills, orders, and the event log do have `stored_at`. The same `_purge` that drops bars and `live_decisions` drops them at 86400 seconds. After that, reconciliation is the wallet and the position rows. Fills still inside the window can be replayed from initial capital and must match the book. When they do not, because the entry fill has aged out, the book is left as it is. It is not rebuilt from the remaining fills, from `live_decisions`, or by replaying the runner.

Runner memory (cross and bars-since) is not stored. The samples are prices. A new process steps the bars the cursor has already passed, with the current position visible only on bars after `attached_at`, and it does not apply those steps. A restart in the middle of a pyramid can disagree with the backtester on a signal that reads `ENTRY_PRICE()` until the long is flat. The in-process walk does not have that gap.

## Fill model

The fill is the backtester's. Costs are the IR fields `commission_bps` and `slippage_bps`. No other fee is added. The formulas are `docs/backtest.md`: buy at close × (1 + slippage_bps / 10000), sell at close × (1 − slippage_bps / 10000), cent rounding and the `1e-12` quantity grid included. The broker calls that engine for the book update. It does not keep a second cost model.

`fill_price_mode` is `on_close`. The fill price and the fill time come from the closed bar that produced the signal. A market buy or sell uses that bar's close. An OCO leg uses that bar's open, high, and low only, and only when the pair was attached on an earlier bar. The path inside the bar is unknown, so a bar that touches both legs fills the stop. A take-profit fills at its limit. A stop fills at the stop, or at the open when the bar opens through the stop. Sell slippage and commission then match a market sell.

A synthetic bar is not a fill and it does not arm or touch an OCO. Its close may sit in the indicator window, as it does in the backtester. A hole has no high and no low, so it is not a touch either. Neither is queued for the next real bar.

A bar is eligible only when all of these hold:

- it is a real bar, not synthetic and not a hole
- it is closed: `open_time + bar_seconds + 60 <= now` (the live close grace)
- its close, `open_time + bar_seconds`, is strictly after `registered_at`

The order is placed at the bar's close. It expires `ORDER_TTL_SECONDS` (300) after that close. The 300 seconds are wall-clock seconds, not bars. A 1-minute bar and a 1-day bar use the same 300 seconds, measured from the close, not from the open. The close grace is 60 seconds, so a tick that runs when the bar first becomes eligible is inside the window and fills. A tick that arrives more than 300 seconds after the close logs `ORDER_PLACED` and `ORDER_EXPIRED` and does not fill. That fill is not retried. The backtester has no clock, so a late paper tick diverges from it on purpose.

One book has one order in status `open`. A market fill or an expiry finishes the order in the same tick, so a prompt bar does not leave one resting. A second signal while an order is still open is `ORDER_REJECTED` with reason `open_order`. It is not queued.

## Event log

Each eligible bar appends rows the purge will delete. The kinds used here:

| Kind | When |
| --- | --- |
| `SIGNAL_FIRED` | A rule's signal is true on this bar |
| `ORDER_PLACED` | A market order or an OCO leg is sent to the book |
| `ORDER_FILLED` | The engine filled it |
| `ORDER_EXPIRED` | The tick is more than 300 seconds after the bar's close, or a resting order reached its deadline |
| `ORDER_REJECTED` | Synthetic bar, hole, no cash, no position, pyramiding, max positions, or a second open order |
| `LINT` | The engine refused to loosen an OCO pair |

`ORDER_FILLED` carries the derived price, quantity, notional, and commission. Those rows expire with the fill log. The open position keeps its own copy of the entry, the basis, and the OCO levels.
