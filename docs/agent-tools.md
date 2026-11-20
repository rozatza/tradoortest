# Agent tools

The tool layer is the typed surface the author loop calls. It does not call a model itself, and it does not hold a wallet, a key, or a swap path. The model client is `service/app/agent`. This layer only wraps services that already exist.

Code lives in `service/app/tools/`. `create_app()` mounts `GET /tools` and `POST /tools/{tool_name}`.

## Call shape

`GET /tools` returns the catalog in this order:

1. `search_asset`
2. `read_authoring_reference`
3. `write_strategy`
4. `update_strategy`
5. `validate`
6. `backtest_start`
7. `backtest_status`
8. `backtest_get`
9. `add_chart`
10. `add_indicator`

Each descriptor has `name`, `description`, `mutates_state`, `input_schema`, and `output_schema`. Handlers are not part of the catalog.

`POST /tools/{tool_name}` runs one tool for the current user. A successful call is HTTP 200:

```json
{"ok": true, "tool": "validate", "data": {}}
```

A tool that ran and failed uses the same envelope and an HTTP status taken from the error code:

```json
{
  "ok": false,
  "tool": "update_strategy",
  "error": {
    "code": "version_conflict",
    "message": "strategy version conflict",
    "path": "$.expected_version",
    "retryable": false,
    "details": {"current_version": 1, "current_ir_hash": "<sha256>"}
  }
}
```

A bad HTTP body, a body over 1 MiB, or a tool name that is not the catalog uses the existing `{error: {code, message, path}}` envelope. An unknown catalog name is HTTP 404 `unknown_tool`. `NaN`, `Infinity`, and a NUL byte in the raw body are HTTP 422 `invalid_json` or `invalid_string` and never reach a handler.

Every input and output schema is JSON Schema draft 2020-12. Every object schema sets `additionalProperties` to false. Patterns use ECMA-262, so `$` matches only the end of the text. A trailing newline does not pass. Exported schemas keep `$` and do not contain the Python-only `\Z` anchor. The dispatcher rejects unknown fields, a bool used as an integer, non-finite numbers, NUL, a string over 100000 characters, a payload over 1 MiB, and a document deeper than the IR depth cap. `user_id` and `owner_id` are not arguments. Sending either is a schema error.

## Auth

The dispatcher takes the `CurrentUser` from `get_current_user()`. Stateful calls use `user.user_id` and nothing from the arguments, headers, or query string. The HTTP routes return 403 `forbidden` when that user is not `local-user`, which matches the strategy routes.

A strategy or job owned by any other id is `not_found`. The response does not include that row's hash or any sign that the id exists. There is no signup, role, or share.

## Errors

| Code | HTTP | Retry | When |
| --- | --- | --- | --- |
| `not_found` | 404 | no | Missing strategy, version, or job, including another owner's row. |
| `strategy_archived` | 404 | no | The strategy is archived. |
| `unknown_tool` | 404 | no | The name is not in the catalog. |
| `version_conflict` | 409 | no | `expected_version` or `expected_ir_hash` is not the row just read. |
| `no_changes` | 409 | no | Apply was asked to store a candidate identical to that row. |
| `conflict` | 409 | no | `request_key` was already used with a different backtest body. |
| `expired` | 410 | no | The backtest result is past the 24 hour TTL. |
| `payload_too_large` | 413 | no | The body, or one string, is over the cap. |
| `validation_error` | 422 | no | The arguments or the strategy document are not valid. |
| `invalid_mint`, `no_pool`, `no_dex_pool_yet` | 422 | no | Asset resolution failed for that reason. |
| `unknown_symbol` | 422 | no | `add_chart` or `add_indicator` named a symbol that is not a major. |
| `confirmation_required` | 422 | no | `update_strategy` apply without `confirm: true`. |
| `expr_too_deep`, `invalid_string`, `non_finite_number` | 422 | no | The JSON value itself is not acceptable. |
| `rate_limited`, `limit_reached` | 429 | yes | The asset source cap, or the backtest rate or concurrency cap. |
| `service_unavailable` | 503 | yes | The SQLite file is locked, corrupt, or unwritable, or the upstream asset source failed. |
| `internal_error` | 500 | no | Anything else. The message is fixed. |

Messages are single-line and capped. They do not include a traceback, an exception class name, a filesystem path, SQLite text, an environment value, or a secret. A failed open does not stick: the next call reads the environment again.

`validate` is the exception to the failure envelope. A strategy that does not compile is still `ok: true`, with `data.valid` false and `diagnostics` filled. A malformed tool argument, such as both `dsl` and `ir`, stays `ok: false`.

## Preview before apply

`update_strategy` compiles the candidate first. The default `mode` is `preview`.

Preview returns `mutated: false`, `from_version`, `to_version` (`from_version + 1`), both hashes, `changed`, `dsl_diff`, and `ir_diff`. It does not insert a version and it does not change `updated_at`. `confirm` and `expected_version` are ignored in this mode.

`mode: "apply"` writes only when `confirm` is true and `expected_version` is the version just read. Optional `expected_ir_hash` must match that same row. The check runs before the diff is built and before `add_version`. A stale `expected_version` cannot return a diff for version 1 while a later version is committed. `StrategyStore.add_version` compares the same pair again inside the `BEGIN IMMEDIATE` transaction that inserts the next version. A mismatch raises `VersionConflict`, rolls back, and returns `version_conflict` with the owner's current version and hash. The response `from_version`, `from_ir_hash`, `dsl_diff`, and `ir_diff` describe that predecessor. Two overlapping applies with the same `expected_version` produce one new version and one conflict.

When the candidate DSL and IR are both identical to that predecessor, apply returns `no_changes` and does not append a version.

`PUT /strategies/{strategy_id}` does not send `expected_version` or `expected_ir_hash`, and it does not use the `no_changes` guard. Omitting both keeps that route appending the next version, which is the ROZ-75 behavior. The author loop's accept path is separate and is described in `docs/agent-loop.md`.

## Tools

`search_asset` resolves a symbol or an SPL mint. An exact major symbol, or a major mint, never calls the network. Any other mint uses `AnyMintSource.resolve_mint` and the `pool_resolutions` row. A cache hit is `cached: true`. The row expires on the existing 24 hour OHLCV TTL. This tool does not write OHLCV bars. `mutates_state` is false because no strategy or job changes. The pool row is the same 24 hour cache the ingest path already uses.

`read_authoring_reference` returns one topic: `overview`, `dsl`, `expressions`, `indicators`, `bar_sizes`, `ir_schema`, or `examples`. `examples` may name `rsi-cross`, `ema-cross`, or `bollinger-revert`. There is no path argument. Text comes from `docs/dsl-spec.md`, `schemas/strategy-ir-v1.schema.json`, and `schemas/examples/`. Output is capped at 48000 characters and reports `truncated` and `omitted_chars`.

`write_strategy` takes `dsl` or `ir`, compiles with the Phase 1 pipeline, and stores version 1 for the current user. An invalid document writes nothing.

`validate` uses that same compile path and does not touch the store.

`backtest_start` accepts a saved `strategy_id` (optional `version`) or inline `dsl` or `ir`, plus `bars` and an optional `request_key`. The author loop pins the version it validated before this tool runs; see `docs/agent-loop.md`. The body is prepared by `app.jobs.routes._prepare` and started on `JobQueue`. A missing `request_key` is a new id. The same key and body returns the original job. A different body for that key is `conflict`. `backtest_status` and `backtest_get` read that job for the current user. `started_at` and `finished_at` are null because the queue does not store them. `created_at` and `expires_at` come from the row and the 24 hour result TTL. A finished job's bars are not kept. Results live only in the job result row until that TTL. The backtest engine is unchanged.

`add_chart` checks a symbol, mint, or asset against the major registry or `require_mint`, and checks `timeframe` against `BAR_SIZES`. A symbol that is not a major is `unknown_symbol`. That code comes from the chart and indicator directive helpers, not from `search_asset`. It returns a `directive_type` of `chart` and `directive_version` `"1.0"`. It stores nothing.

`add_indicator` accepts `sma`, `ema`, `atr`, `rsi`, `macd`, `bollinger`, and `stochastic`. Parameters match `INDICATOR_PARAMS` and the Phase 1 bounds: periods are integers from 1 to 500, RSI period starts at 2, and Bollinger `stddev` is in `(0, 10]`. A bool is rejected. MACD does not require `fast` to be less than `slow`, which is the Phase 1 rule. The default pane is `overlay` for `sma`, `ema`, and `bollinger`, and `separate` for the rest. An explicit pane is allowed. Warmup comes from the indicator library. The directive id is sha256 of the canonical JSON, so the same arguments always return the same id. Nothing is stored.

## What this layer does not do

- No model call of its own. The author loop in `service/app/agent` is the model client. This layer still does not read an API key.
- No wallet, signing, swap, or order routing.
- No second user, signup, or sharing.
- No change to backtest fill math, the ROZ-72/73 performance guards, or the 24 hour TTL constants.
- No OHLCV bars or backtest results outside the existing TTL stores.
- No change to `web/`.
