# Paper broker

`service/app/paper` turns a registered strategy into simulated fills. It is one book per `(user, strategy id, version)`: that version's initial capital, cash, and open longs. `user_id` is the auth seam. This process still has one user. There is no key, no signature, no chain, and no order is sent.

The scheduler in `docs/live-feed.md` records decisions with an empty book. This broker does not read those decisions. A bar the scheduler back-decides on the first tick was already closed at registration. Its close time is at or before `registered_at`, and the broker does not fill it.

When the scheduler is running, one tick is ordered: the feed fetches, the store writes the closed bars, the decision walk runs, then `PaperBroker.tick` reads that store. The broker does not fetch. A direct `PaperBroker.tick` only sees bars already stored.

## Deploy and undeploy

`POST /strategies/{strategy_id}/versions/{version}/paper` deploys one saved version. The body is ignored. The IR is the canonical document of that version, copied onto the registration at request time. A later `PUT` appends a new version and does not change the running document. `registered_at` is the bar-store clock at the first deploy. The same version again is HTTP 200 and does not move `registered_at`.

`DELETE` on the same path undeploys it. The registration is deleted, so the scheduler and the broker no longer evaluate that version.

Undeploy does not invent a fill. A broker tick that has already decided a bar still has to pass the registration check inside the commit. If undeploy removed the row first, that commit writes no fill, no order, no event, and it does not move the cursor.

The deploy route and the OHLCV runtime open the same file: `ohlcv.sqlite` under `OHLCV_DATA_DIR`, or `data/ohlcv.sqlite` when that variable is unset. The running-paper count and the insert are one immediate transaction, so two connections cannot both pass the cap.

- An open position is left as stored. Qty, open cost, entry, basis, realized, and the OCO levels stay. The price fields are not cleared, because the long is not flat. The state is `open_unmanaged`: later bars do not move it until a new deploy of that version. Cash is unchanged.
- A resting order (`status` `open`) is set to `canceled`. No price is written and no fill row is inserted.
- The paper cursor stays where it was. Fills already stored stay until the 24-hour purge.
- The event log appends `ORDER_CANCELED` (reason `undeploy`) for each resting order and one `STRATEGY_UNDEPLOYED` (`position_state` `open_unmanaged` or `flat`, and `orders_canceled`). Those rows purge at 86400 seconds with the other events.

A new deploy of the same version inserts a new registration and a new `registered_at`. The wallet, the position, and the cursor are still there, so the book resumes. Bars whose close is at or before the new `registered_at` are not filled.

The running-paper cap counts registration rows for this user. Each deployed version is one. The default is 3 (`DEFAULT_PAPER_MAX_RUNNING`). `PAPER_MAX_RUNNING` overrides it with a whole number from 1 to 100. A blank value keeps 3. A new version at the cap is HTTP 409 `paper_limit` with message `paper strategy limit reached`. A version that is already running does not take another slot. An unusable setting is HTTP 503 `service_unavailable`. An unknown strategy or version is HTTP 404. A saved IR with no paper feed for its symbol is HTTP 422 `not_deployable`. Undeploy of a version that is not running is HTTP 404 `not_deployed`.

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
- it is stored. A closed bar that is not in the store yet does not move the paper cursor. The tick stops on that open time and logs `not_ready` (`bar not stored`) on the live bar log. It keeps waiting until `now` is past that bar's close plus `ORDER_TTL_SECONDS` (300), the same bound as a late fill. A failed fetch is passed in as `hold` and keeps waiting after that bound, including when the following bar is already closed. If the bar is stored while the cursor is still waiting, the next tick fills it and the fill matches `run_backtest` on the stored series.
- it is closed: `open_time + bar_seconds + 60 <= now` (the live close grace)
- its close, `open_time + bar_seconds`, is strictly after `registered_at`

An empty slot whose fetch succeeded, once `now` is past its close plus 300 seconds, is a hole. The tick appends `BAR_MISSED` (reason `ttl`) and steps that open time with no fill, so a permanent gap cannot freeze the book. A bar that shows up only after that skip is not filled. A bar stored inside the window is filled, including when it arrives two or more bars late.

The order is placed at the bar's close. It expires `ORDER_TTL_SECONDS` (300) after that close. The 300 seconds are wall-clock seconds, not bars. A 1-minute bar and a 1-day bar use the same 300 seconds, measured from the close, not from the open. The close grace is 60 seconds, so a tick that runs when the bar first becomes eligible is inside the window and fills. A tick that arrives more than 300 seconds after the close logs `ORDER_PLACED` and `ORDER_EXPIRED` and does not fill. That fill is not retried. The backtester has no clock, so a late paper tick diverges from it on purpose.

One book has one order in status `open`. A market fill or an expiry finishes the order in the same tick, so a prompt bar does not leave one resting. A second signal while an order is still open is `ORDER_REJECTED` with reason `open_order`. It is not queued.

## Dashboard

`GET /paper` is the book the dashboard shows. The caller is the local user. The body is `mode` `paper` and `simulated` true, then three lists:

- `strategies`: each running registration (`strategy_id`, `version`, `name`, `bar_size`, `registered_at`, `status` `running`). A version that is not registered is not in this list.
- `positions`: each stored position row. `state` is `open` when qty is set and the version is running, `open_unmanaged` when qty is set and it is not, and `flat` when qty is null. `realized` is the stored text. `mark` is null, or `{label: "mark", price, open_time, timeframe, source: "last_stored_close"}` for the newest stored bar that is not synthetic. A flat row has no mark. The response has no cash field and no combined P&L field.
- `events`: each `paper_events` row still inside the 24-hour TTL, including `BAR_MISSED` and `STRATEGY_UNDEPLOYED`. `detail` is the stored text.

The web app polls `GET /api/paper`, which proxies that route. Deploy and undeploy from the dashboard are the same `POST` and `DELETE` as above, proxied at `/api/strategies/{strategy_id}/versions/{version}/paper`.

## Event log

Each eligible bar appends rows the purge will delete. The kinds used here:

| Kind | When |
| --- | --- |
| `SIGNAL_FIRED` | A rule's signal is true on this bar |
| `ORDER_PLACED` | A market order or an OCO leg is sent to the book |
| `ORDER_FILLED` | The engine filled it |
| `ORDER_EXPIRED` | The tick is more than 300 seconds after the bar's close, or a resting order reached its deadline |
| `ORDER_REJECTED` | Synthetic bar, hole, no cash, no position, pyramiding, max positions, or a second open order |
| `BAR_MISSED` | The cursor stepped an empty slot after the order TTL. No fill is written |
| `ORDER_CANCELED` | Undeploy canceled a resting order. No fill is written |
| `STRATEGY_UNDEPLOYED` | The registration was removed. `position_state` is `open_unmanaged` or `flat` |
| `LINT` | The engine refused to loosen an OCO pair |

`ORDER_FILLED` carries the derived price, quantity, notional, and commission. Those rows expire with the fill log. The open position keeps its own copy of the entry, the basis, and the OCO levels.
