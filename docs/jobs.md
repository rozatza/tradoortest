# Backtest jobs

`POST /backtests` runs one strategy for the local user and stores the result. `GET /backtests/{job_id}` is the status. `GET /backtests/{job_id}/results` is the stored result. The engine itself still does not write a file. This API does.

The caller is `get_current_user`. Today that is `local-user`. A job row stores that `user_id`, and a read returns a job only when the id matches. There is no signup and no second user.

## Queue

The queue is a SQLite file, `data/backtest-jobs.sqlite`, and a thread pool in the same process. SQLite is already how this process stores bars. A Redis service would be a second process, a hashed dependency, and a network hop for a tool that has one user. The file keeps a finished result after the worker has exited, so `GET` results still works when the process is started again on the same path.

On open, a job left `queued` or `running` by a process that stopped is marked `failed` with code `interrupted`. It does not keep a concurrency slot.

Default limits are 2 jobs queued or running, and 30 new jobs per minute. A repeated `request_key` for the same user returns the original job and does not consume either limit. The first body is the one that runs.

Tests pass their own database path. Importing the app, and `GET /health`, do not create the file.

## Start

`POST /backtests` takes a JSON object:

| Field | Meaning |
| --- | --- |
| `request_key` | A non-empty string of at most 200 characters. The idempotency key. |
| `ir` or `dsl` | Exactly one. `ir` is strategy IR. `dsl` is strategy-file text. |
| `bars` | Object of bar lists, keyed by asset id. Each bar has `open_time` (integer unix second) and `close`. `open`, `high`, `low`, and `volume` default from `close`. `synthetic: true` is a flat no-trade minute. |
| `long_no_trade_minutes` | Optional. Default 120. The quiet-run cut from `docs/ohlcv.md`. |

A new job is HTTP 202. The same `request_key` is HTTP 200 and the same `job_id`. The body is:

```json
{"job_id": "...", "request_key": "...", "user_id": "local-user", "status": "queued", "error": null}
```

`status` is `queued`, `running`, `succeeded`, or `failed`.

Validation runs before the job is stored. A bad document, a lone surrogate, a document nested past the JSON depth limit, and a time grid above 200,000 bars are HTTP 422 and do not create a job. The grid count is `(last - first) / bar_seconds + 1`. Two 1-minute bars a year apart are over the cap. The error object is `{code, message, path}`.

`concurrency_limit` and `rate_limit` are HTTP 429 with that same object.

## Results

`GET /backtests/{job_id}/results` is HTTP 200 when the job succeeded. The body is `tradoortest.backtest_result.v1`:

- `final_equity` and `final_cash` are decimal strings
- `metrics.values` is one decimal string or null per ratio
- `metrics.undefined` maps a null ratio to its reason (`docs/metrics.md`)
- `metrics` also carries the grid, missing, synthetic, and suspect-outage counts, the in-position twins, and `take_profit_limit_below_low`
- `trades` and `fills` use the same decimal strings as the trade log

No field is NaN or infinite. A job that is still `queued` or `running` is HTTP 409 `not_ready`. A `failed` job is HTTP 422 with the stored error. Unknown jobs are HTTP 404.

A failed run uses a code, not a 500. That includes `grid_too_large`, `expr_too_deep`, `indicator_error`, `indicator_overflow`, `indicator_non_finite`, and `invalid_string`. `RecursionError` at the route is HTTP 422 `expr_too_deep`.

The result is the row written when the job succeeded. Opening the database again returns the same JSON.
