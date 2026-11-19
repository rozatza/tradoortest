# Backtest metrics and the trade log

`run_backtest` returns `metrics` and `trades` on the same result as the fills. `trade_log_csv` and `trade_log_json` encode the trades. `metrics_csv` and `metrics_json` encode the ratios. The formulas below are what the tests lock. Ratios are `Decimal` values or null, half-even to `1e-10`, computed in a fresh 50-digit context. The context is not copied from the caller. Its precision is 50, rounding is half-even, the exponent range is the full decimal range, and the only traps are `InvalidOperation`, `DivisionByZero`, and `Overflow`. The caller's precision, traps, and `Emax` do not change a ratio and cannot put NaN in the output. A ratio the definition leaves undefined is null, and `metrics.undefined` maps that field to a reason. The reasons are `too_few_returns`, `zero_variance`, `no_downside`, `no_drawdown`, `no_losses`, `no_wins`, `no_closed_trades`, `zero_initial`, `no_bars`, `too_short`, `annualization_overflow`, and `overflow`. `annualization_overflow` is only the annualized return and Calmar. A net return, Sharpe, Sortino, win rate, or payoff that the `1e-10` quantize cannot hold is `overflow`. A defined zero stays 0. No metric and no trade-log field is NaN or infinite, and a ratio never raises out of the run.

The risk-free rate is 0. A 365-day year is the clock: `bars_per_year = 365 * 24 * 3600 // bar_seconds`. A 1m bar is 525600, a 5m bar is 105120, a 1d bar is 365. Every `engine.bar_size` divides the year evenly.

USDC cents and the sale-amount tie are in `docs/backtest.md`. Fills stay there. This page is the report.

## Equity and returns

`initial` is the starting capital, half-even to the cent. The equity curve is one cent value after each grid slot, warmup included. `E_0` is `initial`. `E_i` is the curve point for slot `i`. `N` is the number of slots (`bar_count`). The final equity is `E_N`, or `initial` when the grid is empty.

The bar return is the simple return

```text
r_i = (E_i - E_{i-1}) / E_{i-1}
```

for `i = 1..N`. If `E_{i-1}` is 0, that return is 0. The division is skipped. These returns are the Sharpe and Sortino inputs. Each return, and the sums of those returns, are carried at 50 decimal digits. When every return is the same 50-digit value, the variance is 0 and Sharpe is `zero_variance`. Averaging those equal returns a second time leaves a remainder near `1e-50`, and that remainder is not a variance. The published ratio is still the half-even quantize of that result to `1e-10`. On the hand series the `1e-10` digit matches the exact rational. Max drawdown walks the cent integers of the curve and quantizes the exact drop.

**Net return** is `(final - initial) / initial`. It uses the last mark, so an open long is in the number. Flat equity is a real 0. If `initial` quantizes to 0 the reason is `zero_initial`. An empty grid is `no_bars`. A net return the `1e-10` quantize cannot hold is null, reason `overflow`.

**Annualized return** is `(1 + net) ^ (bars_per_year / N) - 1`. The exponent is `bars_per_year / N`, not `N + 1`. A run shorter than one day (`bar_count * bar_seconds < 86400`) is not annualized: the value is null and the reason is `too_short`. 60 one-minute bars are an hour. 1440 one-minute bars, or one daily bar, are a full day and are annualized. `net = 0` on a run of at least one day is a real 0. A base of 0 or less (the account is at or through zero) is `-1`. The half-even quantize to `1e-10` at 50 digits cannot hold a ratio whose absolute value reaches `1e40`. That result is null and the reason is `annualization_overflow`. It is not a crash, and it is not a reported 0.

**Max drawdown** is the largest peak-to-trough drop on the path `initial, E_1, …, E_N`, as a positive fraction `(peak - equity) / peak`. The peak starts at `initial` and moves up only when equity is strictly higher. A peak of 0 does not divide. If equity never drops, drawdown is 0.

## Risk ratios

`N` here is the number of bar returns, which is `bar_count`.

**Sharpe** is `mean(r) / s * sqrt(bars_per_year)`. `s` is the sample standard deviation: the square root of `sum((r - mean)^2) / (N - 1)`. The mean and both sums are 50-digit decimals. That is `ddof = 1`, not the population divisor `N`. `N < 2` is null, reason `too_few_returns`, and the value is null rather than 0. A zero variance is null, reason `zero_variance`. A ratio the quantize cannot hold is null, reason `overflow`. A short 1m run still gets a Sharpe. `sqrt(525600)` can make an hour-long Sharpe a large number. That is this formula, not an annualized return.

**Sortino** uses the same mean and the same `sqrt(bars_per_year)`. The downside deviation is `sqrt(sum(min(r, 0)^2) / (N - 1))`. Every return is in the sum. A positive return contributes 0. The divisor is `N - 1`, not the count of negative returns. The target is 0, the same assumption as the risk-free rate. `N < 2` is `too_few_returns`. No negative return is null, reason `no_downside`.

**Calmar** is the reported annualized return divided by the reported max drawdown, quantized again to `1e-10`. It uses the annualized return, not the net return. A drawdown of 0 is null, reason `no_drawdown`. If the annualized return is null, Calmar is null for the same reason (`too_short`, `annualization_overflow`, `overflow`, `zero_initial`, or `no_bars`). A Calmar the quantize cannot hold is `annualization_overflow`.

The square root, the power, and the divisions run in the local 50-digit half-even context. The final quantize is half-even at `1e-10`. A failure of that quantize is the null above. It does not become `BacktestError`.

## Closed trades

A trade is one long from flat to flat. A pyramid and a partial sell stay on that same trade. A long still open at the last bar is a separate trade with `status = open`.

Closed trades are in exit order. An open trade is not a win, not a loss, and not part of a streak.

| Realized pnl | Meaning |
| --- | --- |
| `> 0` | win |
| `< 0` | loss |
| `0` | scratch. It is in the win-rate denominator, it is in neither payoff average, and it resets both streaks |

**Win rate** is wins / closed trades. Zero closed trades is null, reason `no_closed_trades`. Closed trades that include no win are a real 0, including a book of only scratches and losses.

**Payoff ratio** is the average win divided by the average absolute loss. Scratches are in neither average, and a scratch is not a loss. No closed trades is `no_closed_trades`. Closed trades and no wins is `no_wins`. Wins and no losses is `no_losses`. A payoff the quantize cannot hold is `overflow`.

**Win streak** and **loss streak** are the longest runs of wins and of losses in that exit order.

Realized pnl is booked at each sell from the basis open at that fill. The basis is the open notional plus the buy commission still attached to the open quantity. A sell removes `sold / open_qty` of that basis. Realized increases by

```text
sell_notional - sell_commission - basis_removed
```

A later buy does not change realized already booked. The reported cent is the half-even cent of that exact sum. A trade that never sold has realized pnl `0.00`.

Unrealized pnl on a quantity still open is `remaining_qty * last_mark - remaining_basis`, half-even to the cent. The remaining basis includes the buy commission still open. A closed trade reports `0.00`. The last mark is that asset's last close on the grid. An open trade's unrealized pnl is already inside final equity, and therefore inside net return. It is not inside win rate, payoff, or the streaks.

Across the trades, realized plus unrealized equals final equity minus initial. Before that rounding, the exact sum of booked realized pnl and open `qty * mark - basis` equals the unrounded equity minus the initial capital. The run raises `BacktestError` when that identity fails, including a miss below one cent. It is not absorbed.

After the identity holds, each open trade's realized and unrealized pnl are half-even to the cent, and final equity is already a cent. The gap between those cents and `final - initial` is at most one cent per open trade. The gap is a whole number of cents. Each cent is added to, or removed from, one open trade's unrealized pnl. The trade chosen is the one whose unrounded unrealized pnl sits farthest from its rounded cent in the direction of the gap (largest remainder). A tie goes to the earlier open trade, the one with the smaller trade id. No open trade is moved by more than one cent. Closed trades are not adjusted. A gap larger than one cent per open trade raises `BacktestError`.

## Edge cases

| Case | Value |
| --- | --- |
| No closed trades | Win rate and payoff are null, `no_closed_trades`. Both streaks are 0 |
| `N < 2` | Sharpe and Sortino are null, `too_few_returns` |
| Zero variance (`N >= 2`, every return equal) | Sharpe is null, `zero_variance`. Sortino is `no_downside` when none of those returns are negative |
| No negative return | Sortino is null, `no_downside` |
| No drawdown | Max drawdown is 0. Calmar is null, `no_drawdown`, when the run is long enough to annualize |
| Wins and no losses | Payoff is null, `no_losses` |
| Closed trades and no wins | Payoff is null, `no_wins`. Win rate is 0 |
| Scratch | Realized `0.00`. It is in the win-rate denominator, in neither payoff average, and it resets both streaks |
| Open at the end | `status = open`. Exit time and exit price are null. Left out of win rate, payoff, and streaks. Net return still includes the mark |
| Flat equity | Net return is 0. Annualized return is 0 when the run is at least one day, and null `too_short` when it is not |
| Run shorter than one day | Annualized return and Calmar are null, `too_short` |
| `initial` quantizes to `0.00` | Net return, annualized return, Sharpe, Sortino, and Calmar are null, `zero_initial` |
| Grid has no slots | `bar_count` is 0. Those return ratios are null, `no_bars`. Final equity is the initial cent. Drawdown is 0 |
| Total loss (`1 + net <= 0`) on a run of at least one day | Annualized return is `-1` |
| Ratio at or beyond `1e40` | Annualized return and Calmar are null, `annualization_overflow`. Net return, Sharpe, Sortino, win rate, and payoff are null, `overflow`. The run still returns |
| Exactly 1440 one-minute bars | A full day. Annualized return is computed. 1439 one-minute bars are `too_short` |

## Data quality

The counts are asset-slots, summed across assets. `grid_bars` is the clock length and is not multiplied by the number of assets. Minutes are `bars * bar_seconds / 60`.

A quiet slot:

- On `1m`, a quiet slot is a `synthetic` bar. A 1m hole is missing. It is not quiet, and it breaks a synthetic run.
- On any other bar size, a hole after that asset's first real bar is quiet. The engine has no fetched span, so it cannot tell an unfetched gap from a native no-trade bucket. A hole before the first real bar is not quiet. A synthetic bar is quiet on every bar size.

A run of consecutive quiet slots is a long no-trade when its minutes are strictly greater than `long_no_trade_minutes` (default 120, the same cut as `docs/ohlcv.md`). Those slots are the suspect-outage set. `suspect_outage` is true when the set is not empty. `suspect_outage_bars` equals `long_no_trade_bars`, and the minute fields match the same way. The bars are not removed.

In-position counts use the same slots, restricted to bars where that asset was open at the bar's start, open at the bar's end, or had any fill on the bar. A same-bar round trip counts once. A trade's own counts are the slots from its entry time through its exit time, inclusive, or through the last grid slot when it is still open. Two trades can share a bar.

`missing_bars` counts holes. `synthetic_bars` counts flagged synthetic bars. Both have an in-position twin.

### Indicator lint

Indicators are not masked. A finite output that reads a suspect-outage sample adds a lint and the series is left as computed:

```text
{series_id} on {asset_id} is computed across {count} suspect_outage bars
```

`count` is the number of suspect-outage slots that fed a finite value.

- Rolling (`sma`, `bollinger` period, `stochastic` as `k_period + d_period - 1`): a finite output whose window overlaps the set. The overlapping slots are counted once.
- Seeded (`ema`, `rsi`, `atr`, `macd`): a finite source run that contains a suspect-outage slot and still produces a finite output. Every suspect-outage index in that run counts. ATR and stochastic use `close` to find the run, because a hole is NaN there.

A window that is only NaN does not lint. A 5m hole already makes SMA NaN, so a long hole with no finite output depending on it sets `suspect_outage` and does not add this lint. A 1m synthetic run does sit in the close series. An SMA that uses those closes is linted, and the signal still sees the stale value.

## Take-profit below the bar's low

A take-profit fills at its limit even when the bar opens above that limit. `Fill.limit_below_low` is true when the raw limit, before sell slippage, is strictly below that bar's low. The fill price is still the limit. `metrics.take_profit_limit_below_low` counts those fills. A trade's `limit_below_low` is true when any of its sells was such a fill. A stop, a market sell, and a take-profit whose limit is at or above the low are false.

## Trade log

`trade_log_csv(trades)` and `trade_log_json(trades)` encode the same cells. JSON is an object with `schema` equal to `tradoortest.trade_log.v1` and `trades`, an array of objects. CSV uses the header below, one row per trade, `\n` line endings, and no index column. An empty log is the header and `"trades": []`. Bools are `true` and `false`. A null is JSON `null` and an empty CSV cell. That is how an open trade's `exit_time` and `exit_price` are written. Every other JSON field is a string, and that string is the CSV cell. The same bytes come back under any `PYTHONHASHSEED`.

`metrics_csv` is one row whose header is `net_return`, `annualized_return`, `max_drawdown`, `sharpe`, `sortino`, `calmar`, `win_rate`, `payoff_ratio`. `metrics_json` is `schema` `tradoortest.metrics.v1`, `values` with those keys, and `undefined` mapping a null field to its reason. A null ratio is JSON `null` and an empty CSV cell. A defined ratio is `format(ratio, "f")`, so a zero is `0.0000000000` and not `0E-10`.

| Column | Type in the file | Unit and rounding |
| --- | --- | --- |
| `trade_id` | integer string, from 1 | Exit order, then still-open trades |
| `asset_id` | string | IR asset id |
| `status` | `closed` or `open` | |
| `entry_time` | integer string | Unix seconds, the entry fill's bar open time |
| `exit_time` | integer string, or null | Unix seconds of the bar that closed the long. Null when the trade is open |
| `qty` | decimal string | Closed entry quantity, or the quantity still open. 50 significant digits, not re-rounded to a cent |
| `entry_price` | decimal string | Quantity-weighted average of the entry fill prices, 50 significant digits |
| `exit_price` | decimal string, or null | Same for the sells. Null when nothing was sold |
| `entry_notional` | money | USDC cents, two decimal places. Sum of buy notionals |
| `exit_notional` | money | USDC cents. Sum of sell notionals, `0.00` when nothing was sold |
| `commission` | money | USDC cents. Buy commission plus sell commission |
| `realized_pnl` | money | USDC cents. The closed-trade formula above |
| `unrealized_pnl` | money | USDC cents. `0.00` when the trade is closed |
| `limit_below_low` | `true` or `false` | Any take-profit on this trade filled below the bar's low |
| `overlaps_suspect_outage` | `true` or `false` | The trade's span contains a suspect-outage slot |
| `synthetic_bars` | integer string | Synthetic slots in the trade's span |
| `missing_bars` | integer string | Holes in the trade's span |
| `long_no_trade_bars` | integer string | Suspect-outage slots in the span |
| `long_no_trade_minutes` | integer string | Those slots as minutes |
| `suspect_outage_bars` | integer string | Same count as `long_no_trade_bars` |
| `suspect_outage_minutes` | integer string | Same count as `long_no_trade_minutes` |

Money is half-even to `0.01` from the exact rational, then rendered with two decimal places. Prices, `qty`, and ratios use `format(decimal, "f")` so the digit string is not scientific notation.
