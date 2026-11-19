# Backtest metrics and the trade log

`run_backtest` returns `metrics` and `trades` on the same result as the fills. `trade_log_csv` and `trade_log_json` encode those trades. The formulas below are what the tests lock. Ratios are `Decimal` values, half-even to `1e-10`. A ratio the definition leaves undefined is `0`. No metric and no trade-log field is NaN or infinite.

The risk-free rate is 0. A 365-day year is the clock: `bars_per_year = 365 * 24 * 3600 // bar_seconds`. A 1m bar is 525600, a 5m bar is 105120, a 1d bar is 365. Every `engine.bar_size` divides the year evenly.

USDC cents and the sale-amount tie are in `docs/backtest.md`. Fills stay there. This page is the report.

## Equity and returns

`initial` is the starting capital, half-even to the cent. The equity curve is one cent value after each grid slot, warmup included. `E_0` is `initial`. `E_i` is the curve point for slot `i`. `N` is the number of slots (`bar_count`). The final equity is `E_N`, or `initial` when the grid is empty.

The bar return is the simple return

```text
r_i = (E_i - E_{i-1}) / E_{i-1}
```

for `i = 1..N`. If `E_{i-1}` is 0, that return is 0. These returns are the Sharpe and Sortino inputs. They are not re-rounded before the ratio.

**Net return** is `(final - initial) / initial`. It uses the last mark, so an open long is in the number. If `initial` is 0, net return is 0.

**Annualized return** is `(1 + net) ^ (bars_per_year / N) - 1`. `N = 0` or `net = 0` is 0. A base of 0 or less (the account is at or through zero) is `-1`. If the power overflows the 50-digit context, the result is 0.

**Max drawdown** is the largest peak-to-trough drop on the path `initial, E_1, …, E_N`, as a positive fraction `(peak - equity) / peak`. The peak starts at `initial` and moves up only when equity is strictly higher. A peak of 0 does not divide. If equity never drops, drawdown is 0.

## Risk ratios

`N` here is the number of bar returns, which is `bar_count`.

**Sharpe** is `mean(r) / s * sqrt(bars_per_year)`. `s` is the sample standard deviation: the square root of `sum((r - mean)^2) / (N - 1)`. That is `ddof = 1`, not the population divisor `N`. `N < 2` or a zero variance is 0.

**Sortino** uses the same mean and the same `sqrt(bars_per_year)`. The downside deviation is `sqrt(sum(min(r, 0)^2) / (N - 1))`. Every return is in the sum. A positive return contributes 0. The target is 0, the same assumption as the risk-free rate. `N < 2` or no negative return is 0.

**Calmar** is the reported annualized return divided by the reported max drawdown, quantized again to `1e-10`. A drawdown of 0 is 0.

The square root and the power use the 50-digit half-even context. The final quantize is half-even at `1e-10`.

## Closed trades

A trade is one long from flat to flat. A pyramid and a partial sell stay on that same trade. A long still open at the last bar is a separate trade with `status = open`.

Closed trades are in exit order. An open trade is not a win, not a loss, and not part of a streak.

| Realized pnl | Meaning |
| --- | --- |
| `> 0` | win |
| `< 0` | loss |
| `0` | scratch. It is in the win-rate denominator, it is in neither payoff average, and it resets both streaks |

**Win rate** is wins / closed trades. Zero closed trades is 0.

**Payoff ratio** is the average win divided by the average absolute loss. Scratches are in neither average. No wins or no losses is 0.

**Win streak** and **loss streak** are the longest runs of wins and of losses in that exit order.

Realized pnl, for the quantity that was sold, is

```text
(exit_notional - sell_commission) - (entry_notional + buy_commission) * (sold / entry_qty)
```

rounded half-even to the cent from that exact value. A trade that never sold has realized pnl `0.00`.

Unrealized pnl on a quantity still open is `remaining_qty * last_mark - remaining_cost`, half-even to the cent. A closed trade reports `0.00`. The last mark is that asset's last close on the grid. An open trade's unrealized pnl is already inside final equity, and therefore inside net return. It is not inside win rate, payoff, or the streaks.

## Edge cases

| Case | Value |
| --- | --- |
| No closed trades | Win rate, payoff, and both streaks are 0 |
| Zero variance (`N >= 2`, every return 0) | Sharpe and Sortino are 0 |
| No negative return | Sortino is 0 |
| No drawdown | Max drawdown and Calmar are 0 |
| No losing closed trade, or no winning one | Payoff is 0 |
| Scratch | Realized `0.00`, not a win, both streaks reset |
| Open at the end | `status = open`. Exit time and exit price are empty in the log. Left out of win rate, payoff, and streaks. Net return still includes the mark |
| `initial` quantizes to `0.00` | Net return, annualized return, Sharpe, Sortino, and Calmar are 0 |
| Grid has no slots | `bar_count` is 0. The ratios above are 0. Final equity is the initial cent |
| Total loss (`1 + net <= 0`) | Annualized return is `-1` |
| Annualized power overflows | Annualized return is 0, and Calmar follows that 0 |

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

`trade_log_csv(trades)` and `trade_log_json(trades)` encode the same strings. JSON is an object with `schema` equal to `tradoortest.trade_log.v1` and `trades`, an array of objects. CSV uses the header below, one row per trade, `\n` line endings, and no index column. An empty log is the header and `"trades": []`. Bools are `true` and `false`. An empty exit field is `""` in both. Every JSON field is a string, so a cell-by-cell compare against the CSV row is exact.

| Column | Type in the file | Unit and rounding |
| --- | --- | --- |
| `trade_id` | integer string, from 1 | Exit order, then still-open trades |
| `asset_id` | string | IR asset id |
| `status` | `closed` or `open` | |
| `entry_time` | integer string | Unix seconds, the entry fill's bar open time |
| `exit_time` | integer string or `""` | Unix seconds of the bar that closed the long |
| `qty` | decimal string | Closed entry quantity, or the quantity still open. 50 significant digits, not re-rounded to a cent |
| `entry_price` | decimal string | Quantity-weighted average of the entry fill prices, 50 significant digits |
| `exit_price` | decimal string or `""` | Same for the sells. Empty when nothing was sold |
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

Money is half-even to `0.01` from the exact rational, then rendered with two decimal places. Prices and `qty` use `format(decimal, "f")` so the 50-digit value is the full digit string, not a scientific-notation shorthand.
