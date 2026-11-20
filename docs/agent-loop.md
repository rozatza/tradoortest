# Author agent loop

`service/app/agent/` is the author loop for one local user. It calls the ROZ-76 dispatcher. It does not reimplement the ten tools, their schemas, pool resolution, or the backtest engine. It does not hold a wallet, a key, a signature, or a swap path.

## Provider seam

`LLMProvider.complete` is synchronous. HTTP routes call it from a worker thread.

| Tier | Default model name | What it is for |
| --- | --- | --- |
| `fast` | `fast` | Short tool turns. |
| `balanced` | `balanced` | The default session tier. |
| `max` | `max` | The largest configured model. |

`StubProvider` returns a script and does not open a socket. CI uses it. `OpenAICompatibleProvider` posts to `{base}/chat/completions` with `Authorization: Bearer`. `AnthropicProvider` posts to `{base}/v1/messages` with `x-api-key` and `anthropic-version: 2023-06-01`. Tool schemas are the exported ECMA-262 documents from `export_specs()`. `update_strategy` is annotated so the model is told that an edit becomes a pending proposal.

API keys are read from the environment only: `AGENT_API_KEY`, then `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` / `XAI_API_KEY`. A missing key is `provider_not_configured` and no request is sent. A 429 is `rate_limited`. A timeout or any other HTTP failure is `service_unavailable`. The provider body, URL, and key are not copied into the error. A success body is redacted before it is parsed, so an echoed key does not land in the transcript.

The default provider is `stub`, so an unset environment never dials the network.

## Environment

`.env.example` lists the names and leaves every value empty.

| Name | Default |
| --- | --- |
| `AGENT_DB_PATH` | `./data/agent.sqlite` |
| `AGENT_MAX_BODY_BYTES` | `65536` (1 to 1048576) |
| `AGENT_BUSY_TIMEOUT_MS` | `5000` (0 to 60000) |
| `AGENT_DEFAULT_TIER` | `balanced` |
| `AGENT_PROVIDER` | `stub` |
| `AGENT_PROVIDER_FAST`, `AGENT_PROVIDER_BALANCED`, `AGENT_PROVIDER_MAX` | the shared provider |
| `AGENT_BASE_URL` | OpenAI `https://api.openai.com/v1` or Anthropic `https://api.anthropic.com` |
| `AGENT_BASE_URL_FAST`, `AGENT_BASE_URL_BALANCED`, `AGENT_BASE_URL_MAX` | the shared base URL |
| `AGENT_MODEL_FAST`, `AGENT_MODEL_BALANCED`, `AGENT_MODEL_MAX` | the tier name |
| `AGENT_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY` | unset |

An unknown tier on a request is HTTP 422. A bad provider name, model, base URL, or default tier is HTTP 503 and the response does not echo the value.

## Loop and caps

One user message is one turn. Each provider call is a step. The defaults are 8 steps and 16 tool calls. A request may raise them to 16 and 32. A message is at most 8000 characters. A tool result fed back to the model is capped. Hitting a cap emits `error` with `step_limit_exceeded` or `tool_limit_exceeded`, sets the turn status to `stopped_limit`, then emits `turn_complete`.

The system prompt tells the model that `update_strategy` does not write and that `validate` must pass before `backtest_start`.

## Guardrail 1: no silent overwrite

Any `update_strategy` call is forced to `mode: "preview"` before the dispatcher runs. `confirm`, `expected_version`, and `expected_ir_hash` from the model are removed. The strategy file is not modified during the turn.

A preview with `changed: false` stores no proposal. Apply of that same candidate is `no_changes` (HTTP 409) and does not append a version. `PUT /strategies/{id}` is unchanged and still appends.

A preview with `changed: true` stores a pending proposal: ids, the predecessor version and hash, the diffs, and the canonical candidate DSL. The tool result tells the model that storage was not modified.

`POST /agent/proposals/{id}/accept` is the only write. It claims the row from `pending` to `applying` inside `BEGIN IMMEDIATE`, then calls `update_strategy` with `mode: "apply"`, `confirm: true`, and the predecessor version and hash. The applied `ir_hash` must equal the proposal's `to_ir_hash`. A mismatch is HTTP 500 `internal_error` ("applied strategy does not match the proposal") and the row is not marked `accepted`. A second accept of a decided row returns HTTP 409 `proposal_already_decided` without another write. A stale predecessor is HTTP 409 `version_conflict`; the proposal becomes `conflict` and cannot be accepted or rejected again. `POST /agent/proposals/{id}/reject` marks `rejected` and does not touch the strategy file.

A row left in `applying` is reconciled at the start of the next accept or reject for that proposal. If version `to_version` is stored and its `ir_hash` is `to_ir_hash`, the row becomes `accepted` and accept returns that result without writing again. If that version is missing, the row returns to `pending` and the decision continues. If the version is stored with a different hash, the row becomes `conflict`.

Apply also refuses a predecessor that is not the row just read, before it builds a diff or calls `add_version`. The diff's `from_version` is that row.

## Guardrail 2: validate before backtest

`backtest_start` resolves the canonical `ir_hash` first. A saved strategy is read for the current user. When `version` is omitted, the version read for that hash is copied into the arguments (`strategy_id` and `version`) and that pinned version is what the dispatcher runs. A version saved after the check is not the one that starts. Inline DSL or IR is compiled and not stored. The hash must already have a passing `validate` (`ok: true` and `data.valid: true`) in this session. Otherwise the dispatcher is not called, no job is queued, and the tool result is `validation_required`: "validate must pass for this exact strategy before running a backtest". A later edit has a new hash and needs its own validate.

## Persistence and the 24 hour boundary

`data/agent.sqlite` is gitignored. One connection, an `RLock`, and `BEGIN IMMEDIATE` protect writes. `PRAGMA foreign_keys`, `secure_delete`, `journal_mode=WAL`, and `busy_timeout` are set. Tables are `agent_sessions`, `agent_messages`, `agent_events`, `agent_proposals`, and `agent_validated_hashes`. Every row has `user_id`. There is no users table and no sharing.

`service/app/agent/sanitize.py` runs before tool arguments, tool results, and event payloads are written. The same sanitizer runs on the messages kept in the model context, so bars and backtest results are not sent back to the provider:

- `backtest_start` arguments keep `strategy_id`, `version`, `request_key`, `bar_count`, `symbols`, and `bars_omitted`. The bars stay in the job body.
- `backtest_get` results keep `job_id`, `status`, `ready`, `created_at`, `expires_at`, `error`, and `result_omitted`. The result stays in the job store until its 24 hour TTL.
- `search_asset` results keep `query`, `match_count`, and each match's `kind`, `symbol`, and `mint`, plus `provider_data_omitted`. Pool address, liquidity, and `resolved_at` stay in the 24 hour `pool_resolutions` cache.

Configured API key values are replaced with `[redacted]` before a message or event is stored. A failed open does not stick on `app.state`. A locked, corrupt, or unwritable file is HTTP 503 `service_unavailable` with a fixed message.

## HTTP and SSE

Every agent route uses `CurrentUserDep`. A user other than `local-user` is HTTP 403. A missing row, including another owner's row, is HTTP 404. `user_id` and `owner_id` in a body are unknown fields. The same names in a query string or an `X-User-Id` header are ignored.

| Method | Path | Result |
| --- | --- | --- |
| `POST` | `/agent/sessions` | Create a session. Optional `title`, `tier`, `strategy_id`. |
| `GET` | `/agent/sessions` | A page of this user's sessions. |
| `GET` | `/agent/sessions/{session_id}` | Session, messages, events, and proposals. |
| `GET` | `/agent/sessions/{session_id}/events` | Events with `seq` greater than `after_seq`. |
| `POST` | `/agent/sessions/{session_id}/messages` | One turn. JSON body with `turn_id`, `status`, `events`, `proposals`. |
| `POST` | `/agent/sessions/{session_id}/messages/stream` | The same turn as `text/event-stream`. |
| `GET` | `/agent/proposals` | This user's proposals. Optional `session_id` and `status`. |
| `GET` | `/agent/proposals/{proposal_id}` | One proposal, including the candidate DSL. |
| `POST` | `/agent/proposals/{proposal_id}/accept` | Apply a pending proposal. |
| `POST` | `/agent/proposals/{proposal_id}/reject` | Reject a pending proposal. |

SQLite, the model call, and tool execution run outside the asyncio event loop. `GET /health` stays `{"status": "ok"}` while a turn is blocked, including after a client disconnects mid-turn. The stream worker is a daemon thread. Its join runs in a thread pool, not on the event loop. A declared `Content-Length` or a chunked body over the cap is HTTP 413 and the unread tail is not consumed. An invalid `AGENT_MAX_BODY_BYTES` is HTTP 503 `service_unavailable` with the configured-agent message, not an uncaught 500. `NaN`, `Infinity`, and NUL are HTTP 422. An unexpected exception, including `AssertionError`, is HTTP 500 `internal_error` with the message "the request could not be completed". The body does not include a traceback, a path, or a secret. On the stream, that failure is stored as the next `error` event and a following `turn_complete` when the database can take the write.

An event object has this key order: `seq`, `event_id`, `session_id`, `turn_id`, `type`, `created_at`, `payload`. `seq` starts at 1 and increases inside the session. Types are `user_message`, `tool_start`, `tool_complete`, `proposal_created`, `proposal_decided`, `assistant_message`, `error`, and `turn_complete`.

The stream frame is:

```text
id: <seq>
event: <type>
data: <json>

```

`data` is the same object `GET .../events` returns. A client reconnects with `after_seq`.
