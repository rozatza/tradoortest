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

A market order whose signal is true on that bar is skipped with reason `synthetic_bar`. The order is not queued and it does not fill on the next real bar. The next bar is evaluated on its own. A real bar with volume 0 is still a trade print and can fill. Only the flag blocks the market fill. An OCO leg is not filled on a synthetic bar either. The minute is not a touch, so it does not arm a leg. The next real bar is evaluated on its own.

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
- on a buy, the budget is capped by cash. The room for the position is that budget divided by (1 + commission_bps / 10000), rounded half-even to the cent. Quantity is that room divided by the fill price, rounded down to `1e-12`. Notional is the half-even cent of that quantity times the fill price, and it is at most the room. Commission is the notional times commission_bps / 10000, rounded the same way, and the two never exceed the budget. Unspent dust stays in cash.
- on a sell, notional is quantity × fill price, rounded half-even to the cent. Commission is the notional × commission_bps / 10000, rounded the same way, and it is never more than the notional. Cash increases by notional − commission.

The mark used for sizing is cash plus quantity × that bar's close, rounded half-even to the cent, before the fill. Reported equity uses the same rounding after the fill. The next decision uses that rounded mark. Quantity is not rounded to a cent. At fill time it is rounded down to `1e-12`, a stand-in for a token's base units, so a buy never receives more than its budget can pay for. The cash spent is the cent of the quantity that was actually filled. A budget that cannot buy one unit is skipped (`no_cash`) and spends nothing. A 100% exit sells the open quantity exactly. A partial exit rounds that percent down to the same grid. The average fill price is stored on the fill and read on later bars. It is not divided again on a bar that does not trade.

A second run with the same IR and the same bars returns the same fills, skips, and equity. The engine does not use the clock or a random source.

`slippage_bps` of 10000 or more is refused. A sell would fill at 0. The schema still allows 10000; the engine does not.

An asset with no bars is an error. A derived series that is defined on fewer than half of the grid slots is named in `lints`, with the defined count. Exactly half the slots is not a lint. The run still completes. Sparse 5m data often leaves SMA and the other seeded indicators empty, and the lint is how that shows up. An indicator warning, such as a flat stochastic range, is moved onto `lints`. The warning is not also emitted to the caller. The text names the series and the asset, for example `stoch_k on sol: ...`. A tighten-only rejection is moved onto `lints` the same way. The lint is the `move_oco` message plus the asset and the bar open time, in unix seconds: `tighten_only rejected a looser stop_loss on sol at bar 1767225600`.

USDC amounts use a local 50-digit decimal context and half-even cents. The open quantity is a multiple of `1e-12` and the open cost is the cents spent on what is still open. A cent is half-even of that grid quantity times the price. `5049.495` rounds to `5049.50` and `5049.485` rounds to `5049.48`. The unrounded product `43175 / 78.834 * 1.655514` is `906.675`, which half-even takes to `906.68`. The grid quantity is one `1e-12` unit smaller, the product is `906.6749999999994`, and the cent is `906.67`. A 50-digit rounding of the unrounded quantity would have landed on the same `906.67` from the other side. The hand round trip and the hand OCO pair sit far from a half-cent, and their cents do not move. The caller's decimal context is unchanged after the run. A value that still cannot be priced to the cent raises `BacktestError`. It is not an uncaught decimal error. A hole in the equity curve is marked at the prior close, not at the next bar. A pyramid sizes from marked equity, then caps the spend at cash.

## No look-ahead

The fill on bar `i` uses that bar's close and the signal state built from slots `0..i`. An OCO check on bar `i` uses that bar's open, high, and low only, and only when the pair was attached on an earlier bar. Indicator functions are causal: a NaN hole ends or empties a window, and a later sample does not change an earlier output. Changing or deleting a later bar does not change fills or equity on earlier slots. A test poisons a future close, and a future low through a stop, and fails if an earlier fill moves.

## Worked round trip

Capital 10000.00 USDC. Commission 10 bps. Slippage 5 bps. Enter 50% of equity. The first bar is warmup. The buy is the next bar, close 100. The sell is the bar after, close 130, 100% of the position. The bars are synthetic and live in `service/tests/fixtures/backtest/hand_round_trip.json`. They are not a market print.

Buy fill = 100 × 10005 / 10000 = 100.05.
Budget = 5000.00.
Notional = round half-even of 5000 × 10000 / 10010 = round half-even of 4995.004995… = 4995.00.
Commission = 5000.00 − 4995.00 = 5.00.
Quantity = 4995.00 / 100.05, rounded down to `1e-12`. The cents below are the half-even cents of that grid quantity.
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

A percent smaller than the 50-digit unit of `F` does not move the level. `1e-300` on a fill of 100 leaves both the stop and the target on the entry. The engine rejects that pair with `BacktestError` (`take_profit and stop_loss percents are too small to move the price off the entry`) and does not attach it.

The pair is attached after that fill. It is not checked against the entry bar. A wick through either level on the fill bar does not exit. The next bar is the first one that can. `Fill.leg` is `market` for a rule, and `take_profit` or `stop_loss` for a leg. One fill cancels the other leg.

On a later real bar, only that bar's low and high are read:

- low at or below the stop fills the stop and cancels the take-profit
- otherwise, high at or above the take-profit fills the take-profit and cancels the stop
- if both are touched in that bar, the stop fills and the take-profit is cancelled

The path inside the bar is unknown, so the stop wins. That is the intrabar rule in `docs/dsl-spec.md`.

Prices before sell slippage:

- a take-profit fills at the limit, including when the bar opens above the limit. If that raw limit is strictly below the bar's low, `Fill.limit_below_low` is true and the metrics count it. The fill price is still the limit. `docs/metrics.md` has the flag.
- a stop-loss fills at the stop when the bar opens above the stop
- a stop-loss fills at the open when the bar opens at or below the stop

Sell slippage and commission then match a market sell. The price above is multiplied by `(1 - slippage_bps / 10000)`. Notional is quantity times that price, half-even to the cent. Commission is the notional times `commission_bps / 10000`, half-even, and never more than the notional.

OCO runs before the bar's rules. A stop-out closes the long, and a signal exit on the same bar is `no_position`. An enter on that bar can open a new long. The new pair waits until the next bar. A signal exit that closes the long cancels the pair. A partial exit leaves the pair on the remaining quantity at the same prices.

A synthetic bar is not a fill, and it is not a touch. It does not arm a leg. A missing bar has no high or low, so it is not a touch either. The next real bar uses the live rules: the stop fills when that bar's low is at or below the stop, at the stop or at the open when the bar gaps through, and the take-profit fills at the limit. If that bar touches both, the stop wins. Inserting a no-trade minute does not change the fill. If the run ends on the synthetic bar, the long stays open.

A pyramid does not move the anchor. The anchor stays the first entry fill, including when the added fill is below that anchor and the two enters name different percents. If the added enter names percents that leave both levels where they are, or move the stop up, or move the target down, the levels update. `move_oco` is that check. A lower stop or a higher target rejects the whole change, even when the other leg would have tightened. Neither price moves. The added quantity still fills. The rejection is a lint. The text is what `move_oco` raises, then ` on {asset_id} at bar {open_time}` with the bar open time in unix seconds. A NaN or an infinity raises `BacktestError` (`OCO level is not a finite price`), not `decimal.InvalidOperation`. OCO is settled before the bar's rules, so a tighten or a rejected loosen on this bar is first read on the next bar. The entry bar is the exception: the pair is attached during that bar's rules, and the next bar is still the first check.

The hand-computed pair is synthetic and lives in `service/tests/fixtures/backtest/hand_oco.json`. It is not a market print. Capital 10000. Commission 10 bps. Slippage 5 bps. Enter 100% on the first bar, close 100. Take-profit 10%, stop-loss 5%. The first bar's high is 200 and its low is 1. Those wicks are not read.

Buy fill = 100 × 10005 / 10000 = 100.05.
Notional = round half-even of 10000 × 10000 / 10010 = 9990.01.
Commission = 10000 − 9990.01 = 9.99.
Quantity = 9990.01 / 100.05, rounded down to `1e-12`. The cents below are unchanged.

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

A synthetic bar flat at 90 sits between the entry and the next real bar. It does not touch the stop. The real bar opens at 92, below the stop, so the raw price is the open.
Sell fill = 92 × 9995 / 10000 = 91.954. Notional = 9181.62. Commission = 9.18. Cash = 9172.44.

## What this does not do

The ratios, the drawdown, and the trade log are `docs/metrics.md`. A ratio that is undefined, or that the cent report cannot quantize, is null on that result. It does not fail the run and it does not raise `BacktestError`. The engine does not write a result file and it does not draw a chart. It does not trail a stop except by the tighten-only check above. It does not talk to a venue. Bars come from the caller. How ingest treats missing minutes, synthetic flags, and gaps is `docs/ohlcv.md`.
