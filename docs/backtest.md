# Backtest engine

`service/app/backtest` runs a valid canonical IR document on bars the caller already has. It does not fetch, it does not read the SQLite cache, and it does not send an order. There is no wallet, key, signature, or swap. One run is one book for one user. Nothing here is multi-user.

An entry's `risk` object is one OCO pair. It is attached when the entry fills and can exit the long on a later bar. The rules are below.

## Inputs

`run_backtest(document, bars)` takes the IR object and a map of asset id to `Bar` values. The bar timeframe is `engine.bar_size`. Bars are sorted by `open_time` on that size's grid. A duplicate open time is an error. A missing asset id is an error.

The clock starts at the earliest bar and ends at the latest. Every slot between them is visited, including slots no asset has a bar for.

## When a rule can fire

The only event is `bar_close`. The fill price mode is `on_close`.

`engine.warmup_bars` is a count of grid slots from the start, holes included. Those slots update indicator and signal state. They do not fill.

Indicator outputs follow `docs/dsl-spec.md`. A NaN is empty. A comparison with an empty value is false, so a rule that reads an indicator does not fire until that indicator is defined. The engine calls `service/app/indicators` and does not invent warmup values.

On each slot after warmup, every rule's signal is read against that action's asset, from the book as it stands before any fill on this slot. Rules then run from the top of the array downward. Each true signal invokes its action once. A fill changes the book before the next action, but it does not change the signal values already read for this slot. `ENTRY_PRICE()` on the entry bar is still empty, because the fill has not happened yet. On later bars it is the average fill price of the open long (quote notional still open, divided by quantity).

`AND` and `OR` evaluate both sides. Division by zero is empty. `CROSS_ABOVE` is true when the previous sample was at or below and this sample is above. `CROSS_BELOW` is the opposite. Both samples have to be finite, so the first bar never crosses, and a hole clears the previous sample: the next real bar does not cross against the bar from before the hole. `BARS_SINCE` is 0 on the bar the condition is true, then 1, 2, … on later slots, and empty if it has never been true. Holes count.

## Missing bars

A slot with no bar is a hole.

- Indicator inputs at that slot are NaN. The library treats that as a gap: a rolling window that contains it is NaN, and a seeded indicator (EMA, RSI, ATR, MACD) ends its run. The previous close is not copied into the hole.
- Rules do not fill. A true signal on a hole is skipped with reason `missing_bar`. It is not queued.
- A cross cannot complete across the hole.
- Equity of a position already open is marked at the last close the engine has already seen. That mark is not fed back into the indicators and it is not a fill price. The engine does not read a later bar to price the hole.

Native 5m and higher buckets that the provider omitted show up here as holes, because ingest does not synthesize them. See `docs/ohlcv.md`.

## Synthetic bars

A bar with `synthetic: true` is a flagged 1m no-trade minute from ingest. Its close may sit in the indicator inputs, because the bar exists and the price is the prior real close. It is not a trade print.

A market order whose signal is true on that bar is skipped with reason `synthetic_bar`. The order is not queued and it does not fill on the next real bar. The next bar is evaluated on its own. A real bar with volume 0 is still a trade print and can fill. Only the flag blocks the market fill. An OCO leg that the synthetic bar's high or low would touch is not filled here either. It is deferred to the next real bar, at the price in the OCO section.

## Orders and the book

Orders are market, long-only, spot. A buy opens or adds to a long. A sell closes some or all of that long. A sell while flat is skipped (`no_position`) and does not open a short. Quantity never goes negative. Cash never goes negative.

An enter while that asset is already open is skipped when `allow_pyramiding` is false (`pyramiding`). An enter of a new asset is skipped when the number of open assets is already `max_positions` (`max_positions`).

Sizing:

| Action | Mode | Meaning |
| --- | --- | --- |
| enter | `percent_equity` | That percent of marked equity, in USDC, capped by cash |
| exit | `percent_position` | That percent of the open quantity. 100% closes the long exactly |

Costs, from the IR, in basis points:

- buy fill price = close × (1 + slippage_bps / 10000)
- sell fill price = close × (1 − slippage_bps / 10000)
- on a buy, the cash spent is the budget. Notional is that budget divided by (1 + commission_bps / 10000), rounded half-even to the cent. Commission is the budget minus the notional, so the two sum to the cash that leaves the account.
- on a sell, notional is quantity × fill price, rounded half-even to the cent. Commission is the notional × commission_bps / 10000, rounded the same way, and it is never more than the notional. Cash increases by notional − commission.

The mark used for sizing is cash plus quantity × that bar's close, rounded half-even to the cent, before the fill. Reported equity uses the same rounding after the fill. The next decision uses that rounded mark. Quantity itself is not rounded to a cent.

A second run with the same IR and the same bars returns the same fills, skips, and equity. The engine does not use the clock or a random source.

`slippage_bps` of 10000 or more is refused. A sell would fill at 0. The schema still allows 10000; the engine does not.

An asset with no bars is an error. A derived series that is defined on fewer than half of the grid slots is named in `lints`, with the defined count. Exactly half the slots is not a lint. The run still completes. Sparse 5m data often leaves SMA and the other seeded indicators empty, and the lint is how that shows up. An indicator warning, such as a flat stochastic range, is moved onto `lints`. The warning is not also emitted to the caller. The text names the series and the asset, for example `stoch_k on sol: ...`. A tighten-only rejection is moved onto `lints` the same way.

USDC amounts use a local 50-digit decimal context and half-even cents. The caller's decimal context is unchanged after the run. A value that still cannot be priced to the cent raises `BacktestError`. It is not an uncaught decimal error. A hole in the equity curve is marked at the prior close, not at the next bar. A pyramid sizes from marked equity, then caps the spend at cash.

## No look-ahead

The fill on bar `i` uses that bar's close and the signal state built from slots `0..i`. An OCO check on bar `i` uses that bar's open, high, and low only, and only when the pair was attached on an earlier bar. Indicator functions are causal: a NaN hole ends or empties a window, and a later sample does not change an earlier output. Changing or deleting a later bar does not change fills or equity on earlier slots. A test poisons a future close, and a future low through a stop, and fails if an earlier fill moves.

## Worked round trip

Capital 10000.00 USDC. Commission 10 bps. Slippage 5 bps. Enter 50% of equity. The first bar is warmup. The buy is the next bar, close 100. The sell is the bar after, close 130, 100% of the position. The bars are synthetic and live in `service/tests/fixtures/backtest/hand_round_trip.json`. They are not a market print.

Buy fill = 100 × 10005 / 10000 = 100.05.
Budget = 5000.00.
Notional = round half-even of 5000 × 10000 / 10010 = round half-even of 4995.004995… = 4995.00.
Commission = 5000.00 − 4995.00 = 5.00.
Quantity = 4995.00 / 100.05.
Cash left = 5000.00.
Equity at close 100 = round half-even of 5000 + quantity × 100 = 9992.50.

Sell fill = 130 × 9995 / 10000 = 129.935.
Notional = round half-even of quantity × 129.935 = round half-even of 6487.009745… = 6487.01.
Commission = round half-even of 6487.01 × 10 / 10000 = round half-even of 6.48701 = 6.49.
Cash = 5000.00 + 6487.01 − 6.49 = 11480.52.

The position is flat. Final equity is 11480.52 USDC.

## Take-profit and stop-loss

Every enter carries one OCO pair (`evaluation: on_entry_fill`, `movement: tighten_only`). The levels are anchored to the entry fill `F`, the close plus buy slippage:

- take-profit = `F * (1 + take_profit.percent / 100)`
- stop-loss = `F * (1 - stop_loss.percent / 100)`

The pair is attached after that fill. It is not checked against the entry bar. A wick through either level on the fill bar does not exit. The next bar is the first one that can. `Fill.leg` is `market` for a rule, and `take_profit` or `stop_loss` for a leg. One fill cancels the other leg.

On a later real bar, only that bar's low and high are read:

- low at or below the stop fills the stop and cancels the take-profit
- otherwise, high at or above the take-profit fills the take-profit and cancels the stop
- if both are touched in that bar, the stop fills and the take-profit is cancelled

The path inside the bar is unknown, so the stop wins. That is the intrabar rule in `docs/dsl-spec.md`.

Prices before sell slippage:

- a take-profit fills at the limit, including when the bar opens above the limit
- a stop-loss fills at the stop when the bar opens above the stop
- a stop-loss fills at the open when the bar opens at or below the stop

Sell slippage and commission then match a market sell. The price above is multiplied by `(1 - slippage_bps / 10000)`. Notional is quantity times that price, half-even to the cent. Commission is the notional times `commission_bps / 10000`, half-even, and never more than the notional.

OCO runs before the bar's rules. A stop-out closes the long, and a signal exit on the same bar is `no_position`. An enter on that bar can open a new long. The new pair waits until the next bar. A signal exit that closes the long cancels the pair. A partial exit leaves the pair on the remaining quantity at the same prices.

A synthetic bar is not a fill. If its low or high would touch a leg, that leg is armed and the fill waits for the next real bar. The price is the minimum of that bar's open and the armed level, then sell slippage. A deferred stop never fills above the stop. A deferred take-profit never fills above the limit. A missing bar has no high or low, so it cannot arm a leg. An armed leg stays armed across the hole and still fills on the next real bar. If the run ends on the synthetic bar, the armed leg does not fill and the long stays open.

A pyramid does not move the anchor. The anchor stays the first entry fill. If the added enter names percents that leave both levels where they are, or move the stop up, or move the target down, the levels update. `move_oco` is that check. A lower stop or a higher target rejects the whole change. Neither price moves. The added quantity still fills. The rejection is a lint, and the same text is what `move_oco` raises. OCO is settled before the bar's rules, so a tighten or a rejected loosen on this bar is first read on the next bar. The entry bar is the exception: the pair is attached during that bar's rules, and the next bar is still the first check.

The hand-computed pair is synthetic and lives in `service/tests/fixtures/backtest/hand_oco.json`. It is not a market print. Capital 10000. Commission 10 bps. Slippage 5 bps. Enter 100% on the first bar, close 100. Take-profit 10%, stop-loss 5%. The first bar's high is 200 and its low is 1. Those wicks are not read.

Buy fill = 100 × 10005 / 10000 = 100.05.
Notional = round half-even of 10000 × 10000 / 10010 = 9990.01.
Commission = 10000 − 9990.01 = 9.99.
Quantity = 9990.01 / 100.05.

The next bar trades 100–115 and hits the take-profit only.
Take-profit = 100.05 × 1.10 = 110.055.
Sell fill = 110.055 × 9995 / 10000 = 109.9999725.
Notional = round half-even of quantity × 109.9999725 = 10983.52.
Commission = round half-even of 10983.52 × 10 / 10000 = 10.98.
Cash = 10983.52 − 10.98 = 10972.54.

The same entry with the next bar trading 90–100, opening at 100, hits the stop instead.
Stop = 100.05 × 0.95 = 95.0475.
The open is above the stop, so the fill is the stop, then slippage: 95.0475 × 9995 / 10000 = 94.99997625.
Notional = 9485.76. Commission = 9.49. Cash = 9476.27.

A bar that opens at 80 gaps through that stop, so the raw price is 80 rather than 95.0475.
Sell fill = 80 × 9995 / 10000 = 79.96. Notional = 7984.02. Commission = 7.98. Cash = 7976.04.

A synthetic bar flat at 90 arms the stop and does not fill. The next real bar opens at 92.
Deferred raw price = min(92, 95.0475) = 92.
Sell fill = 92 × 9995 / 10000 = 91.954. Notional = 9181.62. Commission = 9.18. Cash = 9172.44.

## What this does not do

It does not compute a drawdown report or write a result file. It does not trail a stop except by the tighten-only check above. It does not talk to a venue. Bars come from the caller. How ingest treats missing minutes, synthetic flags, and gaps is `docs/ohlcv.md`.
