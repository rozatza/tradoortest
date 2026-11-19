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

The retention window defaults to 24 hours. `JobQueue` takes `result_ttl_seconds` for a shorter window. A value above 24 hours is rejected. It is not clamped. A bool is rejected too. On startup, and on every read and start, a job with `created_at + ttl <= now` is deleted together with its result and any leftover body. A read that finds the job still stored and past the window is HTTP 410 `expired`. A later read of that id is HTTP 404 `not_found`, because the row is gone. A job at 23 hours 59 minutes is still readable. A job at 24 hours 1 minute is not. Exactly 24 hours has reached the window and is deleted.

Keeping results longer than 24 hours waits on ROZ-92, a provider whose terms allow storage.

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

A new job is HTTP 202. The same `request_key` and the same body is HTTP 200 and the same `job_id`. A different body for that key is HTTP 409 `request_key_reused` and does not create a second job. `NaN`, `Infinity`, a magnitude such as `1e400`, and a body that is not UTF-8 are HTTP 422 `invalid_json`. A lone surrogate in `request_key` is HTTP 422 `invalid_string`. A key of 200 characters is accepted. 201 is rejected.

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
- `metrics.values` is one decimal string or null per ratio
- `metrics.undefined` maps a null ratio to its reason (`docs/metrics.md`)
- `metrics` also carries the grid, missing, synthetic, and suspect-outage counts, the in-position twins, and `take_profit_limit_below_low`
- `trades` and `fills` use the same decimal strings as the trade log

No field is NaN or infinite. A job that is still `queued` or `running` is HTTP 409 `not_ready`. A `failed` job is HTTP 422 with the stored error. Unknown jobs are HTTP 404. A job past the retention window is HTTP 410 `expired`, and that read deletes it.

A failed run uses a code, not a 500. That includes `grid_too_large`, `expr_too_deep`, `indicator_error`, `indicator_overflow`, `indicator_non_finite`, and `invalid_string`. `RecursionError` at the route is HTTP 422 `expr_too_deep`.

The result is the row written when the job succeeded. Opening the database again returns the same JSON until the retention window ends. Startup deletes a row that has already ended.
