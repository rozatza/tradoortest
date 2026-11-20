# Backtest jobs

`POST /backtests` runs one strategy for the local user and stores the result. `GET /backtests/{job_id}` is the status. `GET /backtests/{job_id}/results` is the stored result. The engine itself still does not write a file. This API does.

The caller is `get_current_user`. Today that is `local-user`. A job row stores that `user_id`, and a read returns a job only when the id matches. There is no signup and no second user.

## Queue

The queue is a SQLite file, `data/backtest-jobs.sqlite`, and a thread pool in the same process. SQLite is already how this process stores bars. A Redis service would be a second process, a hashed dependency, and a network hop for a tool that has one user. The file keeps a finished result after the worker has exited, so `GET` results still works when the process is started again on the same path, until the retention window ends.

Run one process. Opening the file takes an exclusive lock on `data/backtest-jobs.sqlite.lock`. A second process, including a second uvicorn worker, is refused: it would otherwise mark the first process's live jobs `interrupted`. The lock is released when the process exits, including a crash, so the next process can open the file and mark those jobs interrupted.

On open, a job left `queued` or `running` by a process that stopped is marked `failed` with code `interrupted`. It does not keep a concurrency slot. The request body is deleted at the same time.

The request body, OHLCV bars included, is kept only while the job is `queued` or `running`. It is deleted when the job succeeds, when it fails, and when it is marked interrupted. That row is not a bar cache.

## Retention

CoinGecko API Terms section 6 does not allow a persistent store of API data. The terms are at <https://www.coingecko.com/en/api_terms>. The same note is in `docs/ohlcv.md`. A job result carries fill prices, the trade log, and metrics computed from those bars, so it follows the GeckoTerminal cache.

The retention window defaults to 24 hours. `JobQueue` takes `result_ttl_seconds` for a shorter window. A value above 24 hours is rejected. It is not clamped. A bool is rejected too. On startup, and on every read and start, a job with `created_at + ttl <= now` is deleted together with its result, its chart window, and any leftover body. The same pass deletes a result, a chart window, or a body whose job row is already gone. A worker that finishes after that purge writes nothing: the result is stored only when the job is still `running` in that write. A read that still finds the expired job is HTTP 410 `expired`, and that read deletes it. 410 is best-effort. Another start or read purges the row first, and so does startup, so the client can see HTTP 404 `not_found` without a 410. A later read of an id that was deleted is 404 as well. A job at 23 hours 59 minutes is still readable. A job at 24 hours 1 minute is not. Exactly 24 hours has reached the window and is deleted.

ROZ-92 did not lengthen this window. OHLCV from every provider, including a Birdeye fallback, also expires within 24 hours. See `docs/ohlcv.md`.

Default limits are 2 jobs queued or running, and 30 new jobs per minute. The minute is the open 60 seconds before now: a job created exactly 60 seconds ago no longer counts, and one created 59 seconds ago still does. Queued jobs count toward the concurrency cap, not only jobs already `running`. A repeated `request_key` with the same body returns the original job and does not consume either limit. The same key with a different body is HTTP 409 `request_key_reused`.

Tests pass their own database path. Importing the app, and `GET /health`, do not create the file.

## Start

`POST /backtests` takes a JSON object:

| Field | Meaning |
| --- | --- |
| `request_key` | A non-empty string of at most 200 characters. The idempotency key. |
| `ir` or `dsl` | Exactly one. `ir` is strategy IR. `dsl` is strategy-file text. |
| `bars` | Object of bar lists, keyed by asset id. Each bar has `open_time` (integer unix second) and `close`. `open`, `high`, `low`, and `volume` default from `close`. `synthetic: true` is a flat no-trade minute. |
| `long_no_trade_minutes` | Optional. Default 120. The quiet-run cut from `docs/ohlcv.md`. |

A new job is HTTP 202. The same `request_key` and the same body is HTTP 200 and the same `job_id`. A different body for that key is HTTP 409 `request_key_reused` and does not create a second job. `NaN`, `Infinity`, a magnitude such as `1e400`, and a body that is not UTF-8 are HTTP 422 `invalid_json`. JSON is UTF-8. UTF-16, with or without a BOM, is the same 422. A lone surrogate in `request_key` is HTTP 422 `invalid_string`. A key of 200 characters is accepted. 201 is rejected.

A write that cannot get the database lock is HTTP 503 `busy`, not 500.

The body is:

```json
{"job_id": "...", "request_key": "...", "user_id": "local-user", "status": "queued", "error": null}
```

`status` is `queued`, `running`, `succeeded`, or `failed`.

Validation runs before the job is stored. A bad document, a lone surrogate (including a low surrogate and a surrogate used as an object key), a non-finite number, a document nested past the JSON depth limit, and a time grid above 200,000 bars are HTTP 422 and do not create a job. Depth 96 is accepted. Depth 97 is `expr_too_deep`. A grid of exactly 200,000 bars is accepted. 200,001 is `grid_too_large`, and that check runs before the grid is allocated. The grid count is `(last - first) / bar_seconds + 1`. Two 1-minute bars a year apart are over the cap. The error object is `{code, message, path}`.

`concurrency_limit` and `rate_limit` are HTTP 429 with that same object.

## Results

`GET /backtests/{job_id}/results` is HTTP 200 when the job succeeded. The body is `tradoortest.backtest_result.v1`:

- `final_equity` and `final_cash` are decimal strings
- `equity_curve` is one point per grid slot, in order: `open_time` (integer unix second), `equity` and `cash` (decimal strings). Those are the engine's cent marks. A reader does not recompute them. `backtest_get` removes this field before the tool result is returned. A stored result that has no curve is still HTTP 200, and the four exports below are still HTTP 200
- `metrics.values` is one decimal string or null per ratio
- `metrics.undefined` maps a null ratio to its reason (`docs/metrics.md`)
- `metrics` also carries the grid, missing, synthetic, and suspect-outage counts, the in-position twins, and `take_profit_limit_below_low`
- `trades` and `fills` use the same decimal strings as the trade log

## Export

These four reads encode the stored result. They do not run the engine again and they do not recompute a ratio:

| Path | Body |
| --- | --- |
| `GET /backtests/{job_id}/exports/trades.csv` | stored trades, CSV, with text cells neutralised |
| `GET /backtests/{job_id}/exports/trades.json` | `trade_log_json` of the stored trades |
| `GET /backtests/{job_id}/exports/metrics.csv` | `metrics_csv` of the stored ratios |
| `GET /backtests/{job_id}/exports/metrics.json` | `metrics_json` of the stored ratios |

JSON exports are the same bytes as `trade_log_json` and `metrics_json` for that stored result. They are not formula-escaped. CSV exports are the same bytes as `trade_log_csv` and `metrics_csv` when every cell is a strict decimal, a validated integer, or text that does not start with a formula character.

Every CSV cell, including `trades.csv` and `metrics.csv`, is written by `csv_cell`. There is no text-column list that leaves the other columns raw. A cell is copied unchanged only in two cases. The text matches the strict decimal grammar: an optional minus, an integer part of `0` or a non-zero digit followed by digits, an optional fraction of one or more digits, and no exponent. That is the same grammar as `format(Decimal, "f")` and `DECIMAL_MARK` in `web/lib/backtest.ts`, so `-12.34` stays `-12.34`. Or the column is a declared integer column (`entry_time`, `exit_time`, and the bar and minute counts) or a declared ISO timestamp column, and the text matches that format. No export column is an ISO timestamp today. Any other cell that starts with `=`, `+`, `-`, `@`, a tab, or a carriage return is written with a leading apostrophe. The same apostrophe is written when those characters, or the fullwidth lookalikes `＝` `＋` `－` `＠`, follow a leading space, tab, newline, NBSP, or BOM. A real decimal, including `-12.34`, has no pad and no lookalike, so it stays raw. A column that is not one of those two kinds, including a column added later, takes that path. `=1+1` in a trade price or in `metrics.csv` is escaped. `+12.34` is escaped, because the grammar has no leading plus. JSON exports keep the stored characters, with no apostrophe.

A cell that is not already a string or null is HTTP 422 `invalid_result`. The status codes otherwise match `/results`: 200, 409 `not_ready`, 422 for a failed job, 404, and 410 `expired` when that read is the one that deletes the row. An unknown file name is HTTP 404 `not_found` and does not read the job. `cache-control` is `no-store`.

## Charts

`GET /backtests/{job_id}/bars`, `GET /backtests/{job_id}/indicators`, and `GET /backtests/{job_id}/analysis` are reads for the results screen. They are not tools. `backtest_get` does not return them, and they are not added to the result document.

The request body is still deleted when the job finishes, so the raw series is not kept as that body. Re-reading the OHLCV cache would be a different series: a job runs on the bars the caller sent, including synthetic bars, and those bars are not an OHLCV cache key. The worker keeps a window in `job_charts` (`bars_json` and `indicators_json`), separate from `result_json`. The window is at most 2000 grid slots. It is the earliest slice that covers the most trade-log `entry_time` and `exit_time` values, or the last 2000 slots when no trade time sits on the grid. A grid that is already shorter is stored whole. Present candles in that slice are the bars. Missing slots are not filled with a price.

`bars_json` and `indicators_json` are deleted with the job, on the same 24 hour pass, including a chart row whose job is already gone. The purge checkpoints the WAL, and `secure_delete` is on, so the candle text does not stay in the database file or the WAL after that pass.

Indicator points are the lists from `app.indicators`, called on the same grid the engine uses (a missing slot is NaN). The chart route does not recompute them in the browser. A non-finite indicator value is JSON `null`. `valid` counts the finite points in the window. `shown` is the window length. `total` is the full grid length for a series, and the full present-candle count for an asset. `rejected` on an asset is a present candle in the window that was not encoded.

These reads do not parse `result_json` in Python. Bars and indicators are the chart columns. Analysis asks SQLite for `json_extract(result_json, '$.trades')` and parses that array only. The equity curve stays in the result row.

A succeeded job with no chart row is HTTP 200 with `available: false`. A job that is queued or running is HTTP 409 `not_ready`. A failed job is HTTP 422. An unknown job is HTTP 404. A job past the retention window is HTTP 410 `expired` when that read deletes it. `cache-control` is `no-store`.

## Time of day and weekday

`GET /backtests/{job_id}/analysis` counts trade-log rows. The clock is `entry_time`, a unix second, read as UTC.

| Field | Meaning |
| --- | --- |
| `hour_utc` | Hour of that UTC instant, 0–23 |
| `weekday_utc` | Monday 0 through Sunday 6 (`datetime` weekday of that UTC instant) |
| `weekday` | The English name of `weekday_utc` |
| `count` | How many counted rows fall in the bucket |

A row with a missing `entry_time`, or an `entry_time` that is not a non-negative whole second, is not counted. `trades_used` is the counted rows. `trades_total` is the length of the trade log. Hours and weekdays with no counted row are left out. There is no zero bucket and no 24-hour or 7-day filler. When `trades_used` is 0, `available` is false and the body has no `hours` list and no `weekdays` list. The counts are not returns.

No field is NaN or infinite. A job that is still `queued` or `running` is HTTP 409 `not_ready`. A `failed` job is HTTP 422 with the stored error. Unknown jobs are HTTP 404. A job past the retention window is HTTP 410 `expired` when that read is the one that deletes it. 410 is best-effort: if a start, another read, or startup already purged the row, the response is 404.

A failed run uses a code, not a 500. That includes `grid_too_large`, `expr_too_deep`, `indicator_error`, `indicator_overflow`, `indicator_non_finite`, and `invalid_string`. `RecursionError` at the route is HTTP 422 `expr_too_deep`.

The result is the row written when the job succeeded. Opening the database again returns the same JSON until the retention window ends. Startup deletes a row that has already ended.
