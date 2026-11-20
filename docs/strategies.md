# Saved strategies

`POST /strategies` stores one strategy for the local user. Each later save appends an immutable version. The file is SQLite, `data/strategies.sqlite`, unless `STRATEGIES_DB_PATH` points somewhere else. `data/` and `*.sqlite` are gitignored.

The row stores the owner, the DSL text, the canonical IR JSON, and `ir_hash`. It does not store OHLCV bars or backtest fills. Those stay in the 24-hour cache described in `docs/ohlcv.md` and the job result described in `docs/jobs.md`. A `bars` field on a strategy request is ignored.

The caller is `get_current_user` (`docs/design-notes.md`). Today that is `local-user`. A `user_id` in the body is ignored. There is no signup, role, or share link. A request whose current user is not `local-user` is HTTP 403 `forbidden`.

Importing the app and `GET /health` do not create the database. The first strategy route opens it. Opening is serialized with `_init_lock`. The store is assigned to `app.state.strategies` only after the file is ready. A bad path, a corrupt file, or a bad env value leaves that attribute unset, so the next request can open again.

## Routes

| Method and path | Result |
| --- | --- |
| `POST /strategies` | Create the strategy and version 1. HTTP 201. `Location` is `/strategies/{strategy_id}`. |
| `GET /strategies` | List the caller's active strategies. |
| `GET /strategies/{strategy_id}` | Metadata plus the latest version, including DSL, canonical IR, and `ir_hash`. |
| `PUT /strategies/{strategy_id}` | Append the next version. Version numbers are `1`, `2`, `3`, and so on. Older rows are not updated. |
| `DELETE /strategies/{strategy_id}` | Archive. HTTP 200 with metadata and `archived: true`. |
| `GET /strategies/{strategy_id}/versions` | Version summaries, lowest number first. |
| `GET /strategies/{strategy_id}/versions/{version}` | One version, including DSL and IR. |
| `GET /strategies/{strategy_id}/diff?from=1&to=2` | DSL unified diff, structural IR diff, and both hashes. |
| `POST /strategies/{strategy_id}/validate` | Compile the body and write nothing. HTTP 200 `{valid, strategy_id, name, ir_hash}` when it passes. |

`strategy_id` is 32 lowercase hex characters generated for the row. It is not `strategy.id` from the IR. The IR id is `ir_strategy_id` and can change on a later version. The URL id does not change.

`PUT` is the only save. Sending the same DSL again still creates a new version.

## Body

`POST` and `PUT` take a JSON object. Send exactly one of `dsl` or `ir`.

| Field | Meaning |
| --- | --- |
| `dsl` | Strategy-file text. Stored as submitted. Parsed with `parse_strategy`. |
| `ir` | Strategy IR object. Canonicalized, then stored as the DSL `format_strategy` prints. |
| `user_id` | Ignored. |
| `bars` | Ignored. Not written. |

Any other field is ignored, except the concurrency pair below. `null` for `dsl` or `ir` counts as omitted.

`PUT` may also send `expected_version` and `expected_ir_hash` together. `expected_version` is a whole number from 1 to 1000000. `expected_ir_hash` is 64 lowercase hex characters. Sending only one of them is HTTP 422 `invalid_request` and writes nothing. Sending neither keeps the previous append behavior, except when the request header `x-tradoor-client` is `web`. That header, which the Next save proxy sets, requires the pair. It is not an authentication check. `POST /strategies/{strategy_id}/validate` ignores the pair.

Validation uses the Phase 1 parser and `validate_ir`. There is no second grammar. An invalid document is HTTP 422 with `{error: {code, message, path, diagnostics}}`. `diagnostics` is at most 20 items of `{code, message, path}` and, for a DSL error, `line` and `column`. Messages are single-line and at most 200 characters. Nothing is inserted.

A valid save stores:

- the DSL text
- canonical IR JSON (`canonical_json`, UTF-8, no insignificant whitespace)
- `ir_hash`, the lowercase sha256 of that JSON (`app.ir.canonical.ir_hash`)

A comment, a blank line, or a reordered section changes the stored DSL and leaves `ir_hash` the same. A logic change, such as a different `warmup_bars`, changes `ir_hash`. The hash does not depend on key insertion order or `PYTHONHASHSEED`.

## List, versions, and archive

`GET /strategies` returns active strategies for the current user, ordered by `created_at` ascending and then `strategy_id`. Each item has metadata and the latest `ir_hash`. It does not include DSL or IR.

| Query | Default | Rule |
| --- | --- | --- |
| `limit` | 50 | Whole number from 1 to 100. |
| `offset` | 0 | Whole number from 0 to 10000. |
| `include_archived` | `false` | Only the strings `true` and `false`. |

`total` is the count before the page. An offset past the end is HTTP 200 and an empty page. The same `limit` and `offset` rules apply to `GET /strategies/{strategy_id}/versions`, which is ordered by version number ascending.

`DELETE` sets `archived_at`. The version rows stay in the file and are not served. A later `GET`, `PUT`, version list, version read, diff, or second `DELETE` is HTTP 404 `not_found`, the same as an unknown id. `GET /strategies?include_archived=true` still lists the row, with `archived: true` and no DSL. `include_archived` does not change get-by-id.

A strategy owned by any other `user_id` is invisible. The running process does not have a way to become that user.

## Diff

`GET /strategies/{strategy_id}/diff?from=1&to=2` reads two stored versions. `from` and `to` are required version numbers from 1 to 1000000. The same number is allowed. Either direction is allowed. A missing version is HTTP 404 `not_found`.

```json
{
  "strategy_id": "...",
  "from_version": 1,
  "to_version": 2,
  "from_ir_hash": "<64 lowercase hex chars>",
  "to_ir_hash": "<64 lowercase hex chars>",
  "dsl_diff": "--- v1\n+++ v2\n...",
  "ir_diff": [
    {"op": "replace", "path": "$.engine.warmup_bars", "from": 26, "to": 30}
  ]
}
```

`dsl_diff` is a unified diff in the same form as `diff -u`. Identical DSL is an empty string. Otherwise the header lines are `--- vN` and `+++ vN`. A source line with no trailing newline is followed by `\ No newline at end of file`.

`ir_diff` walks the canonical IR. Object keys are sorted. List indexes are numbers in the path, so `$.rules[2]` sorts before `$.rules[10]`. `op` is `replace` (`from` and `to`), `add` (`value`), or `remove` (`value`). Identical versions produce an empty list. The same two documents produce the same list under any `PYTHONHASHSEED`.

## Errors

Strategy routes return `{"error": {"code", "message", "path"}}` for the statuses in the table below. `path` is a JSONPath or null. Those bodies never include a traceback, a filesystem path, or SQLite's own text.

A path this router does not define, and a method it does not allow, are FastAPI's `{"detail": ...}` (HTTP 404 or 405). A malformed `Content-Length` header can be rejected by the server with a plain HTTP 400 before the application runs. The 400 `invalid_request` row is what the application returns when that header reaches it.

| HTTP | `code` | When |
| --- | --- | --- |
| 400 | `invalid_request` | `Content-Length` is not a decimal integer. |
| 403 | `forbidden` | The current user is not `local-user`. |
| 404 | `not_found` | Unknown id, archived strategy, another owner's row, or missing version. |
| 409 | `version_conflict` | `expected_version` or `expected_ir_hash` is not the latest row. Nothing was written. The error adds `current_version` and `current_ir_hash`. |
| 413 | `payload_too_large` | The declared size or the streamed body is over the cap. |
| 422 | `invalid_json` | The body is not a UTF-8 JSON object. UTF-16, a NUL byte, `NaN`, `Infinity`, and `1e400` are this code. |
| 422 | `invalid_string` | A lone surrogate, or a NUL inside a string. |
| 422 | `invalid_request` | Both or neither of `dsl` and `ir`, or the wrong JSON type. |
| 422 | `invalid_id` | `strategy_id` is not 32 lowercase hex characters. |
| 422 | `invalid_query` | `limit`, `offset`, `from`, `to`, or `include_archived` is outside the rules above. |
| 422 | parser or schema code | DSL or IR failed Phase 1 validation. `diagnostics` is present. Nothing was written. |
| 422 | `expr_too_deep` | The document or an expression is nested too deeply. |
| 503 | `store_unavailable` | The file is locked, corrupt, closed, or unwritable, or a strategy env value is not in range. |
| 500 | `internal` | A failure that is not one of the cases above. The message is fixed. |

A locked or corrupt database is 503 on every strategy route, including a route that would otherwise be 422 for a bad id or a bad body. `GET /health` does not use the file and stays HTTP 200.

The body cap is checked on the declared `Content-Length` and again while reading `request.stream()`, so a chunked request with no `Content-Length` stops at the cap instead of buffering the rest.

SQLite work and parsing run in a thread pool. The connection uses `PRAGMA foreign_keys=ON`, `PRAGMA secure_delete=ON`, and `PRAGMA busy_timeout`. Writes take a process `RLock` and `BEGIN IMMEDIATE`. The next version is `MAX(version) + 1` in that transaction. `(strategy_id, version)` is the primary key, so a version number is not reused. `BEFORE UPDATE` and `BEFORE DELETE` triggers on `strategy_versions` abort with `strategy_versions rows are immutable`.

`StrategyStore.add_version` also accepts optional `expected_version` and `expected_ir_hash`. When either is set, the comparison runs inside the same `BEGIN IMMEDIATE` transaction as the insert. A mismatch raises `VersionConflict` and writes nothing. `PUT /strategies/{strategy_id}` passes the pair when the body includes both fields. It passes neither when they are omitted, unless `x-tradoor-client` is `web`, in which case the missing pair is HTTP 422 and nothing is written. An older client that does not send that header still appends the next version. The editor always sends both and does not retry a 409. The Next PUT proxy refuses a body that lacks a valid pair and does not forward it. The agent `update_strategy` tool is the other caller that sets them. See `docs/agent-tools.md`.

`POST /strategies/{strategy_id}/validate` reads the active row for the current user, then runs the same compiler. A passing document returns `valid: true` and the candidate `ir_hash`. It does not insert a version and it does not change `updated_at`. An invalid document is the same HTTP 422 as a save, including `line` and `column` on a DSL diagnostic. An archived strategy, an unknown id, and another owner's id are HTTP 404.

## Environment

Commented placeholders live in `.env.example`. None of them have a value.

| Variable | Default | Meaning |
| --- | --- | --- |
| `STRATEGIES_DB_PATH` | `data/strategies.sqlite` | SQLite file. A relative path is from the process working directory. |
| `STRATEGIES_MAX_BODY_BYTES` | `65536` | Body cap. Integer from 1 to 1048576. Read on each `POST` and `PUT`, including validate. |
| `STRATEGIES_BUSY_TIMEOUT_MS` | `5000` | Busy timeout. Integer from 0 to 60000. Read when the file is opened. |

An out-of-range value is HTTP 503 `store_unavailable`. It is not stored on `app.state`. The next request reads the environment again.
